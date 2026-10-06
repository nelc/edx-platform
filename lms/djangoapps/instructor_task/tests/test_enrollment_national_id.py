"""
NELC: the async batch-enrollment worker runs the same per-learner code as the synchronous path, so a National ID
typed in the instructor dashboard resolves identically there, and only when the task was submitted by the dashboard.
"""

import json
from unittest.mock import Mock, patch

from django.apps import apps
from django.test import TestCase
from edx_django_utils.cache import RequestCache
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.student.tests.factories import UserFactory
from lms.djangoapps.instructor.enrollment import EmailEnrollmentState
from lms.djangoapps.instructor_task.api import _student_enrollment_task_input, split_enrollment_identifiers
from lms.djangoapps.instructor_task.models import InstructorTask
from lms.djangoapps.instructor_task.tasks_helper.enrollments import process_student_enrollment_batch

HELPER = "lms.djangoapps.instructor_task.tasks_helper.enrollments"


def _fake_enroll(course_key, email, auto_enroll, message_students, params, language=None):  # pylint: disable=unused-argument
    """Stand-in for enroll_email: reports the learner as enrolled."""
    before = EmailEnrollmentState(course_key, email)
    before.user, before.enrollment, before.allowed = True, False, False
    after = EmailEnrollmentState(course_key, email)
    after.user, after.enrollment, after.allowed = True, True, False
    return before, after, None


class TestTaskInputShape(TestCase):
    """The flag is stored in task_input only when set, so every other task keeps its exact shape."""

    def test_flag_absent_by_default(self):
        task_input = _student_enrollment_task_input("enroll", ["a"], True, False, "r", True, None)
        self.assertNotIn("allow_national_id", task_input)  # noqa: PT009

    def test_flag_present_when_set_and_counted_when_splitting(self):
        task_input = _student_enrollment_task_input("enroll", ["a"], True, False, "r", True, None, True)
        self.assertIs(task_input["allow_national_id"], True)  # noqa: PT009
        # splitting measures the larger input, so every chunk still fits once the flag is stored
        chunks = split_enrollment_identifiers("enroll", ["a", "b"], True, False, "r", True, None, True)
        self.assertEqual(chunks, [["a", "b"]])  # noqa: PT009


class TestAsyncWorkerNationalId(TestCase):
    """Run the async worker end to end (enrollment stubbed, report upload and completion email stubbed)."""

    NATIONAL_ID = "1012345678"

    def setUp(self):
        super().setUp()
        try:
            self.extra_info_model = apps.get_model("custom_reg_form", "ExtraInfo")
        except LookupError:
            self.skipTest("custom_reg_form is not installed in this environment")
        self.addCleanup(RequestCache.clear_all_namespaces)
        self.course_key = CourseKey.from_string("course-v1:edX+DemoX+Demo_Course")
        self.requester = UserFactory.create(username="instructor", email="instructor@example.com")
        self.learner = UserFactory.create(username="learner", email="learner@example.com")
        self.extra_info_model.objects.create(user=self.learner, arabic_name="x", national_id=self.NATIONAL_ID)

    def _run_task(self, allow_national_id):
        """Run the async enrollment task for one known and one unknown ID; return its result rows."""
        identifiers = [self.NATIONAL_ID, "2999999999"]
        task_input = _student_enrollment_task_input(
            "enroll", identifiers, False, False, "reason", True, None, allow_national_id
        )
        entry = InstructorTask.objects.create(
            course_id=self.course_key, task_type="student_enrollment_batch", task_key="k",
            task_input=json.dumps(task_input), task_id="t", requester=self.requester,
        )
        with patch(f"{HELPER}.upload_csv_to_report_store") as upload, \
                patch(f"{HELPER}.send_enrollment_task_completion_email"), \
                patch("lms.djangoapps.instructor_task.tasks_helper.runner._get_current_task", return_value=Mock()), \
                patch("lms.djangoapps.instructor.utils.enroll_email", side_effect=_fake_enroll):
            process_student_enrollment_batch(None, entry.id, str(self.course_key), task_input, "enrolled")
        return upload.call_args[0][0]  # the CSV rows

    def test_dashboard_task_resolves_ids_and_reports_the_account(self):
        header, *rows = self._run_task(allow_national_id=True)

        self.assertEqual(header[-2:], ["resolved_email", "resolved_username"])  # noqa: PT009
        by_identifier = {row[0]: dict(zip(header, row)) for row in rows}
        found = by_identifier[self.NATIONAL_ID]
        self.assertTrue(found["success"])  # noqa: PT009
        self.assertEqual(  # noqa: PT009
            (found["resolved_email"], found["resolved_username"]), ("learner@example.com", "learner")
        )
        missing = by_identifier["2999999999"]
        self.assertEqual(missing["error_type"], "national_id_not_found")  # noqa: PT009
        self.assertEqual(missing["error_message"], "No account found with this National ID")  # noqa: PT009

    def test_task_not_flagged_by_the_dashboard_keeps_the_stock_contract(self):
        header, *rows = self._run_task(allow_national_id=False)

        self.assertEqual({row[header.index("error_type")] for row in rows}, {"invalid_identifier"})  # noqa: PT009
