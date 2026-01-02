""" API View for current user's course role """

import edx_api_doc_tools as apidocs
from opaque_keys.edx.keys import CourseKey
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.djangoapps.student.auth import has_studio_read_access
from common.djangoapps.student.roles import get_user_course_role
from openedx.core.lib.api.view_utils import DeveloperErrorViewMixin, verify_course_exists, view_auth_classes

from ..serializers import CourseUserRoleSerializer


@view_auth_classes(is_authenticated=True)
class CourseUserRoleView(DeveloperErrorViewMixin, APIView):
    """
    View for getting the authenticated user's role for a course.
    """

    @apidocs.schema(
        parameters=[
            apidocs.string_parameter("course_id", apidocs.ParameterLocation.PATH, description="Course ID"),
        ],
        responses={
            200: CourseUserRoleSerializer,
            401: "The requester is not authenticated.",
            403: "The requester cannot access the specified course.",
            404: "The requested course does not exist.",
        },
    )
    @verify_course_exists()
    def get(self, request: Request, course_id: str):
        """
        Get the authenticated user's role for the specified course.

        **Example Request**

            GET /api/contentstore/v1/course_user_role/{course_id}

        **Example Response**

        ```json
        { "role": "instructor" }
        ```
        """
        user = request.user
        course_key = CourseKey.from_string(course_id)

        if not has_studio_read_access(user, course_key):
            self.permission_denied(request)

        role = get_user_course_role(user, course_key)
        print(f"role: {role}")

        serializer = CourseUserRoleSerializer({"role": role})
        return Response(serializer.data)

