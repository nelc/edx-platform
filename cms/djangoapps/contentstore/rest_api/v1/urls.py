""" Contenstore API v1 URLs. """

from django.urls import re_path

from openedx.core.constants import COURSE_ID_PATTERN

from .views import (
    CourseDetailsView,
    CourseSettingsView,
    ProctoredExamSettingsView,
)

app_name = 'v1'

urlpatterns = [
    re_path(
        fr'^proctored_exam_settings/{COURSE_ID_PATTERN}$',
        ProctoredExamSettingsView.as_view(),
        name="proctored_exam_settings"
    ),
    re_path(
        fr'^course_settings/{COURSE_ID_PATTERN}$',
        CourseSettingsView.as_view(),
        name="course_settings"
    ),
    re_path(
        fr'^course_details/{COURSE_ID_PATTERN}$',
        CourseDetailsView.as_view(),
        name="course_details"
    ),
]
