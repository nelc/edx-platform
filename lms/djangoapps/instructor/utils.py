"""
Utility functions for student enrollment operations.

This module contains reusable functions for processing student enrollments
that can be used in both synchronous and asynchronous contexts.
"""

import logging
import re
from collections.abc import Callable
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.student.models import (
    ALLOWEDTOENROLL_TO_ENROLLED,
    ALLOWEDTOENROLL_TO_UNENROLLED,
    DEFAULT_TRANSITION_STATE,
    ENROLLED_TO_ENROLLED,
    ENROLLED_TO_UNENROLLED,
    UNENROLLED_TO_ALLOWEDTOENROLL,
    UNENROLLED_TO_ENROLLED,
    UNENROLLED_TO_UNENROLLED,
    CourseEnrollment,
    EnrollStatusChange,
    ManualEnrollmentAudit,
    get_user_by_username_or_email,
)
from lms.djangoapps.instructor.enrollment import (
    enroll_email,
    get_email_params,
    get_user_email_language,
    unenroll_email,
)
from openedx.core.lib.courses import get_course_by_id

log = logging.getLogger(__name__)


User = get_user_model()


def _determine_enroll_state_transition(before_state: dict, after_state: dict) -> str:
    """
    Determine the state transition for an enrollment operation.

    Args:
        before_state (dict): State before enrollment with keys 'enrollment', 'user', 'allowed'
        after_state (dict): State after enrollment with keys 'enrollment', 'allowed'

    Returns:
        str: The state transition constant
    """
    # User was not registered before
    if not before_state["user"]:
        if after_state["allowed"]:
            return UNENROLLED_TO_ALLOWEDTOENROLL
        return DEFAULT_TRANSITION_STATE

    # User was registered and enrolled successfully
    if after_state["enrollment"]:
        if before_state["enrollment"]:
            return ENROLLED_TO_ENROLLED
        if before_state["allowed"]:
            return ALLOWEDTOENROLL_TO_ENROLLED
        return UNENROLLED_TO_ENROLLED

    return DEFAULT_TRANSITION_STATE


def _determine_unenroll_state_transition(before_state: dict) -> str:
    """
    Determine the state transition for an unenrollment operation.

    Args:
        before_state (dict): State before unenrollment with keys 'enrollment', 'allowed'

    Returns:
        str: The state transition constant
    """
    if before_state["enrollment"]:
        return ENROLLED_TO_UNENROLLED
    if before_state["allowed"]:
        return ALLOWEDTOENROLL_TO_UNENROLLED
    return UNENROLLED_TO_UNENROLLED


# NELC: audit transitions that already mean "this learner reached the state the instructor is asking for now".
# ALLOWEDTOENROLL_* are left out on purpose: ALLOWEDTOENROLL_TO_ENROLLED and ALLOWEDTOENROLL_TO_UNENROLLED share the
# same string upstream, so they cannot tell an enroll from an unenroll.
_ALREADY_REACHED_TRANSITIONS = {
    EnrollStatusChange.enroll: (UNENROLLED_TO_ENROLLED, ENROLLED_TO_ENROLLED),
    EnrollStatusChange.unenroll: (ENROLLED_TO_UNENROLLED, UNENROLLED_TO_UNENROLLED),
}


def _was_recently_processed(course_key: CourseKey, email: str, action: str) -> bool:
    """
    NELC: True if the LATEST ManualEnrollmentAudit row shows this learner already reached the state `action` asks
    for, in this course, within BATCH_ENROLLMENT_EMAIL_DEDUPE_SECONDS. Used only to avoid mailing the same learner
    twice when an instructor re-submits a batch (the enrollment itself is idempotent).

    The audit row has no course column, so it is scoped through its `enrollment` FK; invited-but-unregistered
    learners have no enrollment row, so they are never treated as "recently processed".
    """
    window = settings.BATCH_ENROLLMENT_EMAIL_DEDUPE_SECONDS
    if not window:
        return False
    # Only the LATEST row counts: enroll -> unenroll -> enroll again must still notify the second enroll.
    latest = ManualEnrollmentAudit.objects.filter(
        enrolled_email=email,
        enrollment__course_id=course_key,
    ).order_by("-time_stamp", "-id").first()
    return bool(
        latest
        and latest.state_transition in _ALREADY_REACHED_TRANSITIONS.get(action, ())
        and latest.time_stamp  # nullable column
        and latest.time_stamp >= timezone.now() - timedelta(seconds=window)
    )


# NELC: National ID as a batch-enrollment identifier (instructor dashboard only; see _resolve_national_id).
# The shape and the Arabic-Indic digit normalisation mirror eox_nelp.utils.NATIONAL_ID_REGEX / normalize_national_id
# and custom_reg_form's ExtraInfoForm; core must not import either, so the two rules are restated here.
_NATIONAL_ID_PATTERN = re.compile(r"[12][0-9]{9}")
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
NATIONAL_ID_NOT_FOUND = "national_id_not_found"
NATIONAL_ID_AMBIGUOUS = "national_id_ambiguous"


class NationalIdLookupError(Exception):
    """NELC: an ID-shaped identifier that must not be enrolled: no account has it, or it points at two accounts."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _mask_if_national_id(identifier: str) -> str:
    """NELC: identifiers are logged, so an ID-shaped one is reduced to its last four digits."""
    normalized = identifier.strip().translate(_ARABIC_INDIC_DIGITS)
    return f"******{normalized[-4:]}" if _NATIONAL_ID_PATTERN.fullmatch(normalized) else identifier


def _resolve_national_id(identifier: str):
    """
    NELC: resolve an ID-shaped `identifier` to an EXISTING account through custom_reg_form.ExtraInfo.national_id.

    Returns None when the stock lookup should run unchanged: the identifier is not ID-shaped, or the app that
    stores National IDs is not installed. Otherwise returns the matching user or raises NationalIdLookupError.
    Nothing is ever created and no invite is ever made; an unknown ID is an error, not an email.

    An ID-shaped line is only ever a National ID. It is NOT looked up as a username when no account has that ID
    (a typo in an ID must not enrol whoever happens to own that number as a username):
      * exactly one account has the National ID -> that account
      * no account has the National ID          -> not found (even if a username equals the digits)
      * several rows/accounts have the ID, or the digits are ALSO the username of a different account -> ambiguous
    """
    normalized = identifier.strip().translate(_ARABIC_INDIC_DIGITS)
    if not _NATIONAL_ID_PATTERN.fullmatch(normalized):
        return None
    try:
        extra_info_model = apps.get_model("custom_reg_form", "ExtraInfo")
    except LookupError:
        return None

    # The as-typed form is matched too: some stored IDs were saved before normalisation and kept Arabic-Indic digits.
    rows = extra_info_model.objects.filter(
        national_id__in={normalized, identifier}, user__isnull=False
    ).select_related("user")
    id_users = {row.user_id: row.user for row in rows}
    if not id_users:
        raise NationalIdLookupError(NATIONAL_ID_NOT_FOUND, _("No account found with this National ID"))
    try:
        username_user = get_user_by_username_or_email(identifier)
    except User.DoesNotExist:
        username_user = None

    if len(id_users) > 1 or (username_user and username_user.pk not in id_users):
        raise NationalIdLookupError(
            NATIONAL_ID_AMBIGUOUS,
            _("This number matches more than one account (a National ID or a username). "
              "Enter the learner's email address or username instead."),
        )
    return next(iter(id_users.values()))


def process_single_student_enrollment(  # pylint: disable=too-many-statements  # NELC: National ID branch
    request_user: User,
    course_key: CourseKey,
    action: str,
    identifier: str,
    auto_enroll: bool,
    email_students: bool,
    reason: str | None,
    email_params: dict | None,
    allow_national_id: bool = False,
):
    """
    Process enrollment/unenrollment for a single student.

    Args:
        request_user (User): User who initiated the enrollment operation
        course_key (CourseKey): CourseKey object for the course
        action (str): 'enroll' or 'unenroll'
        identifier (str): Student identifier (email or username)
        auto_enroll (bool): Whether to auto-enroll in verified track if applicable
        email_students (bool): Whether to send enrollment emails
        reason (str | None): Optional reason for enrollment change
        email_params (dict | None): Pre-computed email parameters (optional)
        allow_national_id (bool): NELC. Accept a National ID as the identifier of an EXISTING account. Set only by the
            instructor-dashboard request path.

    Returns:
        dict: Result of the enrollment operation with keys:
            - identifier: The student identifier
            - success: Boolean indicating if operation was successful
            - before: State before operation (if successful)
            - after: State after operation (if successful)
            - error_type: Type of error ('invalid_identifier', 'validation_error', 'general_error')
            - error_message: Error message (if failed)
    """
    enrollment_obj = None
    state_transition = DEFAULT_TRANSITION_STATE
    identified_user = None
    email = None
    language = None
    national_id_match = None

    if allow_national_id:  # NELC
        try:
            national_id_match = _resolve_national_id(identifier)
        except NationalIdLookupError as exc:
            return {
                "identifier": identifier,
                "error": True,
                "success": False,
                "error_type": exc.error_type,
                "error_message": exc.message,
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.exception("Error while looking up a National ID for batch enrollment: %s", type(exc).__name__)
            return {
                "identifier": identifier,
                "error": True,
                "success": False,
                "error_type": "general_error",
                "error_message": _("Something went wrong while looking up this National ID. Please try again."),
            }

    try:
        if national_id_match:  # NELC
            identified_user = national_id_match
        else:
            identified_user = get_user_by_username_or_email(identifier)
    except User.DoesNotExist:
        email = identifier
    else:
        email = identified_user.email
        language = get_user_email_language(identified_user)

    try:
        validate_email(email)  # Raises ValidationError if invalid

        # NELC: do not mail a learner twice for the same change (e.g. an instructor re-submitting a batch).
        email_skipped = email_students and _was_recently_processed(course_key, email, action)
        if email_skipped:
            log.info("Skipping duplicate enrollment notification in course %s: learner was just processed", course_key)
            email_students = False

        # Wrap enrollment and audit operations in an atomic transaction
        # to ensure both succeed or both are rolled back
        with transaction.atomic():
            if action == EnrollStatusChange.enroll:
                before, after, enrollment_obj = enroll_email(
                    course_key, email, auto_enroll, email_students, {**email_params}, language=language
                )
                before_state = before.to_dict()
                after_state = after.to_dict()
                state_transition = _determine_enroll_state_transition(before_state, after_state)

            elif action == EnrollStatusChange.unenroll:
                before, after = unenroll_email(
                    course_key, email, email_students, {**email_params}, language=language
                )
                before_state = before.to_dict()
                after_state = after.to_dict()
                state_transition = _determine_unenroll_state_transition(before_state)
                enrollment_obj = (
                    CourseEnrollment.get_enrollment(identified_user, course_key) if identified_user else None
                )

            # Create audit record
            ManualEnrollmentAudit.create_manual_enrollment_audit(
                request_user, email, state_transition, reason, enrollment_obj
            )

        result = {
            "identifier": identifier,
            "before": before_state,
            "after": after_state,
            "success": True,
            "state_transition": state_transition,
        }
        if email_skipped:
            result["email_skipped"] = True  # NELC
        if national_id_match:  # NELC: lets the team match each typed line to the account it resolved to
            result.update(
                resolved_email=identified_user.email,
                resolved_username=identified_user.username,
            )
        return result
    except ValidationError:
        return {
            "identifier": identifier,
            "invalidIdentifier": True,
            "success": False,
            "error_type": "invalid_identifier",
            "error_message": _("Invalid email address"),
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.exception("Error while processing student %s: %s", _mask_if_national_id(identifier), exc)  # NELC
        return {
            "identifier": identifier,
            "error": True,
            "success": False,
            "error_type": "general_error",
            "error_message": _(
                "Something went wrong while processing this learner. Please try again or contact support."
            ),
        }


def process_student_enrollment_batch(
    request_user: User,
    course_key: CourseKey,
    action: str,
    identifiers: list[str],
    auto_enroll: bool,
    email_students: bool,
    reason: str | None,
    secure: bool,
    progress_callback: Callable[..., None] | None = None,
    allow_national_id: bool = False,
):
    """
    Process a batch of student enrollment/unenrollment operations.

    Args:
        request_user (User): User who initiated the batch operation
        course_key (CourseKey): CourseKey object for the course
        action (str): 'enroll' or 'unenroll'
        identifiers (list[str]): List of student identifiers (emails or usernames)
        auto_enroll (bool): Whether to auto-enroll in verified track if applicable
        email_students (bool): Whether to send enrollment emails
        reason (str | None): Optional reason for enrollment change
        secure (bool): Whether the request is secure (HTTPS)
        progress_callback (Optional[Callable]): Optional callback function to report progress
            Should accept (current, total, results) parameters
        allow_national_id (bool): NELC. Accept National IDs (of existing accounts) among the identifiers

    Returns:
        dict: Batch processing results with keys:
            - action: The action performed
            - auto_enroll: Auto-enrollment setting
            - results: List of individual enrollment results
            - successful_operations: Count of successful operations
            - failed_operations: Count of failed operations
            - total_students: Total number of students processed
    """
    email_params = {}
    if email_students:
        course = get_course_by_id(course_key)
        email_params = get_email_params(course, auto_enroll, secure=secure)

    results = []
    successful_operations = 0
    failed_operations = 0
    total_students = len(identifiers)

    for idx, identifier in enumerate(identifiers):
        result = process_single_student_enrollment(
            request_user=request_user,
            course_key=course_key,
            action=action,
            identifier=identifier,
            auto_enroll=auto_enroll,
            email_students=email_students,
            reason=reason,
            email_params=email_params,
            allow_national_id=allow_national_id,  # NELC
        )

        results.append(result)

        if result["success"]:
            successful_operations += 1
        else:
            failed_operations += 1

        if progress_callback:
            progress_callback(idx + 1, total_students, results)

    return {
        "action": action,
        "auto_enroll": auto_enroll,
        "results": results,
        "successful_operations": successful_operations,
        "failed_operations": failed_operations,
        "total_students": total_students,
    }
