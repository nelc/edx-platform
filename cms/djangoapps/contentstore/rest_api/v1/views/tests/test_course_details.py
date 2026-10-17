"""
Unit tests for course details views.
"""
import json
from unittest.mock import patch

import ddt
from django.urls import reverse
from rest_framework import status

from cms.djangoapps.contentstore.tests.utils import CourseTestCase
from openedx.core.djangoapps.models.course_details import CourseDetails

from ...mixins import PermissionAccessMixin


@ddt.ddt
class CourseDetailsViewTest(CourseTestCase, PermissionAccessMixin):
    """
    Tests for CourseDetailsView.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse(
            "cms.djangoapps.contentstore:v1:course_details",
            kwargs={"course_id": self.course.id},
        )

    def test_put_permissions_unauthenticated(self):
        """
        Test that an error is returned in the absence of auth credentials.
        """
        self.client.logout()
        response = self.client.put(self.url)
        error = self.get_and_check_developer_response(response)
        self.assertEqual(error, "Authentication credentials were not provided.")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_put_permissions_unauthorized(self):
        """
        Test that an error is returned if the user is unauthorised.
        """
        client, _ = self.create_non_staff_authed_user_client()
        response = client.put(self.url)
        error = self.get_and_check_developer_response(response)
        self.assertEqual(error, "You do not have permission to perform this action.")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch.dict("django.conf.settings.FEATURES", {"ENABLE_PREREQUISITE_COURSES": True})
    def test_put_invalid_pre_requisite_course(self):
        pre_requisite_course_keys = [str(self.course.id), "invalid_key"]
        request_data = {"pre_requisite_courses": pre_requisite_course_keys}
        response = self.client.put(
            path=self.url,
            data=json.dumps(request_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "Invalid prerequisite course key")

    def test_put_course_details(self):
        request_data = self._course_details_payload()
        response = self.client.put(
            path=self.url,
            data=json.dumps(request_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @ddt.data(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
        "youtu.be/dQw4w9WgXcQ?si=abc",
        "  dQw4w9WgXcQ ",
    )
    def test_put_intro_video_link_is_stored_as_embed_for_the_id(self, pasted):
        """
        A pasted link is normalised to the 11-character ID; the stored embed and the response use only the ID.
        """
        response = self._put({"intro_video": pasted})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["intro_video"], "dQw4w9WgXcQ")
        self.assertIn("/embed/dQw4w9WgXcQ?rel=0", self._stored_about_video())

    @ddt.data("https://vimeo.com/123456789", "abcdefghij", "abcdefghijkl", "not a video")
    def test_put_invalid_intro_video_is_rejected_and_changes_nothing(self, junk):
        self.assertEqual(self._put({"intro_video": "dQw4w9WgXcQ"}).status_code, status.HTTP_200_OK)
        response = self._put({"intro_video": junk, "short_description": "must not be saved"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("YouTube", response.json()["error"])
        self.assertIn("/embed/dQw4w9WgXcQ?rel=0", self._stored_about_video())
        self.assertNotEqual(CourseDetails.fetch(self.course.id).short_description, "must not be saved")

    def test_put_blank_intro_video_clears_the_video(self):
        self._put({"intro_video": "dQw4w9WgXcQ"})
        for blank in (None, "", "  "):
            self.assertEqual(self._put({"intro_video": blank}).status_code, status.HTTP_200_OK)
            self.assertIsNone(self._stored_about_video())
            self._put({"intro_video": "dQw4w9WgXcQ"})

    def _put(self, overrides):
        data = {**self._course_details_payload(), **overrides}
        return self.client.put(path=self.url, data=json.dumps(data), content_type="application/json")

    def _stored_about_video(self):
        return CourseDetails.fetch_about_attribute(self.course.id, "video")

    @staticmethod
    def _course_details_payload():
        return {
            "about_sidebar_html": "",
            "banner_image_name": "images_course_image.jpg",
            "banner_image_asset_path": "/asset-v1:edX+E2E-101+course+type@asset+block@images_course_image.jpg",
            "certificate_available_date": "2029-01-02T00:00:00Z",
            "certificates_display_behavior": "end",
            "course_id": "E2E-101",
            "course_image_asset_path": "/static/studio/images/pencils.jpg",
            "course_image_name": "bar_course_image_name",
            "description": "foo_description",
            "duration": "",
            "effort": None,
            "end_date": "2023-08-01T01:30:00Z",
            "enrollment_end": "2023-05-30T01:00:00Z",
            "enrollment_start": "2023-05-29T01:00:00Z",
            "entrance_exam_enabled": "",
            "entrance_exam_id": "",
            "entrance_exam_minimum_score_pct": "50",
            "intro_video": None,
            "language": "creative-commons: ver=4.0 BY NC ND",
            "learning_info": ["foo", "bar"],
            "license": "creative-commons: ver=4.0 BY NC ND",
            "org": "edX",
            "overview": '<section class="about"></section>',
            "pre_requisite_courses": [],
            "run": "course",
            "self_paced": None,
            "short_description": "",
            "start_date": "2023-06-01T01:30:00Z",
            "subtitle": "",
            "syllabus": None,
            "title": "",
            "video_thumbnail_image_asset_path": "/asset-v1:edX+E2E-101+course+type@asset+block@images_course_image.jpg",
            "video_thumbnail_image_name": "images_course_image.jpg",
            "instructor_info": {
                "instructors": [
                    {
                        "name": "foo bar",
                        "title": "title",
                        "organization": "org",
                        "image": "image",
                        "bio": "",
                    }
                ]
            },
        }
