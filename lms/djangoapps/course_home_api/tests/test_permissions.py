"""
Tests for permissions defined in courseware.rules
"""
import ddt

from common.djangoapps.student.roles import OrgStaffRole, CourseStaffRole
from common.djangoapps.student.tests.factories import UserFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase  # lint-amnesty, pylint: disable=wrong-import-order
from xmodule.modulestore.tests.factories import CourseFactory  # lint-amnesty, pylint: disable=wrong-import-order
from lms.djangoapps.course_home_api.permissions import CAN_MASQUARADE_LEARNER_PROGRESS


@ddt.ddt
class PermissionTests(ModuleStoreTestCase):
    """
    Tests for permissions defined in courseware.rules
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory()
        self.course = CourseFactory(org='org')
        self.another_course = CourseFactory(org='org')

    def tearDown(self):
        super().tearDown()
        self.user.delete()

    @ddt.data(
        (
            True, None, None, True,
            "Global staff users should have masquerade access",
        ),
        (
            False, None, None, False,
            "Non-staff users shouldn't have masquerade access",
        ),
        (
            False, 'another_org', None, False,
            "User with staff access on another org shouldn't have masquerade access",
        ),
        (
            False, 'org', None, True,
            "User with org-wide staff access should have masquerade access",
        ),
        (
            False, None, 'another_course', False,
            "User with staff access on another course shouldn't have masquerade access",
        ),
        (
            False, None, 'course', True,
            "User with staff access on the course should have masquerade access",
        ),
    )
    @ddt.unpack
    def test_can_masquerade_return_value(self, is_staff, org_role, course_role, expected_permission, description):
        """
        Test that only authorized users have masquerade access
        """
        self.user.is_staff = is_staff
        self.user.save()
        assert self.user.is_staff == is_staff

        if org_role:
            OrgStaffRole(org_role).add_users(self.user)

        if course_role:
            CourseStaffRole(getattr(self, course_role).id).add_users(self.user)

        has_perm = self.user.has_perm(CAN_MASQUARADE_LEARNER_PROGRESS, self.course.id)
        assert has_perm == expected_permission, description
