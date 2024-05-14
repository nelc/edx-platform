""" Contenstore API v1 URLs. """

from django.urls import re_path, path

from openedx.core.constants import COURSE_ID_PATTERN

from .views import (
    CourseDetailsView,
    CourseRerunView,
    CourseSettingsView,
    HomePageView,
    ProctoredExamSettingsView,
)

app_name = 'v1'

urlpatterns = [
    path(
        'home',
        HomePageView.as_view(),
        name="home"
    ),
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
    re_path(
        fr'^course_rerun/{COURSE_ID_PATTERN}$',
        CourseRerunView.as_view(),
        name="course_rerun"
    ),
]
