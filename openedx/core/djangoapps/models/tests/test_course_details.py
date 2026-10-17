"""
Tests for CourseDetails
"""


import datetime
from django.test import override_settings
import pytest
import ddt
from pytz import UTC

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from xmodule.modulestore import ModuleStoreEnum
from xmodule.data import CertificatesDisplayBehaviors
from xmodule.modulestore.tests.django_utils import TEST_DATA_SPLIT_MODULESTORE, ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

from openedx.core.djangoapps.models.course_details import ABOUT_ATTRIBUTES, CourseDetails, normalize_youtube_video_id

EXAMPLE_CERTIFICATE_AVAILABLE_DATE = datetime.date(2020, 1, 1)
VIDEO_ID = 'dQw4w9WgXcQ'


@ddt.ddt
class NormalizeYoutubeVideoIdTest(SimpleTestCase):
    """
    The single rule for the course intro video: a YouTube ID or link in, the 11-character ID out.
    """

    @ddt.data(
        VIDEO_ID,
        f'  {VIDEO_ID}\n',
        f'https://www.youtube.com/watch?v={VIDEO_ID}',
        f'https://www.youtube.com/watch?v={VIDEO_ID}&t=30s',
        f'https://www.youtube.com/watch?feature=share&v={VIDEO_ID}',
        f'youtube.com/watch?v={VIDEO_ID}',
        f'http://youtube.com/watch?v={VIDEO_ID}',
        f'https://m.youtube.com/watch?v={VIDEO_ID}',
        f'https://youtu.be/{VIDEO_ID}',
        f'youtu.be/{VIDEO_ID}?si=abcDEF123',
        f'https://youtu.be/{VIDEO_ID}?t=5',
        f'https://www.youtube.com/shorts/{VIDEO_ID}',
        f'youtube.com/shorts/{VIDEO_ID}?feature=share',
        f'https://www.youtube.com/embed/{VIDEO_ID}',
        f'//www.youtube.com/embed/{VIDEO_ID}?rel=0&start=5',
        f'https://www.youtube.com/live/{VIDEO_ID}?feature=share',
        f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}',
        f'  https://WWW.YouTube.com/embed/{VIDEO_ID}/  ',
    )
    def test_accepted_forms(self, value):
        assert normalize_youtube_video_id(value) == VIDEO_ID

    @ddt.data(None, '', '   ', '\n')
    def test_blank_means_no_video(self, value):
        assert normalize_youtube_video_id(value) is None

    @ddt.data(
        'intro',
        'abcdefghij',  # 10 characters
        'abcdefghijkl',  # 12 characters
        'abcdefghij!',
        'abcdef hijk',
        'not a video',
        123456789012,
        ['dQw4w9WgXcQ'],
        'https://vimeo.com/123456789',
        f'https://example.com/watch?v={VIDEO_ID}',
        f'https://youtube.com.evil.example/watch?v={VIDEO_ID}',
        f'https://evil.example/https://www.youtube.com/watch?v={VIDEO_ID}',
        f'ftp://www.youtube.com/watch?v={VIDEO_ID}',
        f'javascript:alert("{VIDEO_ID}")',
        'https://www.youtube.com/watch',
        'https://www.youtube.com/watch?v=short',
        'https://www.youtube.com/watch?v=abcdefghijkl',
        'https://www.youtube.com/',
        'https://www.youtube.com/embed/',
        'https://www.youtube.com/playlist?list=PL1234567890A',
        f'https://www.youtube.com/user/{VIDEO_ID}',
        'https://www.youtube.com/embed/https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        'https://[bad',
    )
    def test_rejected(self, value):
        with pytest.raises(ValidationError):
            normalize_youtube_video_id(value)

    def test_recompose_video_tag_is_guarded(self):
        assert f'/embed/{VIDEO_ID}?rel=0' in CourseDetails.recompose_video_tag(f'youtu.be/{VIDEO_ID}')
        assert CourseDetails.recompose_video_tag(None) is None
        with pytest.raises(ValidationError):
            CourseDetails.recompose_video_tag('junk')

    def test_parse_video_tag_reads_malformed_stored_value_as_empty(self):
        stored_before_validation = (
            '<iframe src="//www.youtube.com/embed/https://www.youtube.com/watch?v=dQw4w9WgXcQ?rel=0"></iframe>'
        )
        assert CourseDetails.parse_video_tag(stored_before_validation) is None
        assert CourseDetails.parse_video_tag(CourseDetails.recompose_video_tag(VIDEO_ID)) == VIDEO_ID


@ddt.ddt
class CourseDetailsTestCase(ModuleStoreTestCase):
    """
    Tests the first course settings page (course dates, overview, etc.).
    """
    MODULESTORE = TEST_DATA_SPLIT_MODULESTORE

    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(default_enrollment_start=True)

    @ddt.data(True, False)
    def test_virgin_fetch(self, should_have_default_enroll_start):
        features = settings.FEATURES.copy()
        features['CREATE_COURSE_WITH_DEFAULT_ENROLLMENT_START_DATE'] = should_have_default_enroll_start

        with override_settings(FEATURES=features):
            course = CourseFactory.create(default_enrollment_start=should_have_default_enroll_start)
            details = CourseDetails.fetch(course.id)
            wrong_enrollment_start_msg = (
                'enrollment_start not copied into'
                if should_have_default_enroll_start
                else f'enrollment_start date somehow initialized {str(details.enrollment_start)}'
            )
            assert details.org == course.location.org, 'Org not copied into'
            assert details.course_id == course.location.course, 'Course_id not copied into'
            assert details.run == course.location.run, 'Course run not copied into'
            assert details.course_image_name == course.course_image
            assert details.start_date.tzinfo is not None
            assert details.end_date is None, ('end date somehow initialized ' + str(details.end_date))
            assert details.enrollment_start == course.enrollment_start, wrong_enrollment_start_msg
            assert details.enrollment_end is None,\
                ('enrollment_end date somehow initialized ' + str(details.enrollment_end))
            assert details.certificate_available_date is None,\
                ('certificate_available_date date somehow initialized ' + str(details.certificate_available_date))
            assert details.syllabus is None, ('syllabus somehow initialized' + str(details.syllabus))
            assert details.intro_video is None, ('intro_video somehow initialized' + str(details.intro_video))
            assert details.effort is None, ('effort somehow initialized' + str(details.effort))
            assert details.language is None, ('language somehow initialized' + str(details.language))
            assert not details.self_paced

    def test_update_and_fetch(self):
        jsondetails = CourseDetails.fetch(self.course.id)
        jsondetails.syllabus = "<a href='foo'>bar</a>"
        # encode - decode to convert date fields and other data which changes form
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).syllabus ==\
                   jsondetails.syllabus, 'After set syllabus'
            jsondetails.short_description = "Short Description"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).short_description ==\
                   jsondetails.short_description, 'After set short_description'
            jsondetails.overview = "Overview"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).overview ==\
                   jsondetails.overview, 'After set overview'
            jsondetails.intro_video = "intro_video"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).intro_video ==\
                   jsondetails.intro_video, 'After set intro_video'
            jsondetails.about_sidebar_html = "About Sidebar HTML"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user)\
                .about_sidebar_html == jsondetails.about_sidebar_html, 'After set about_sidebar_html'
            jsondetails.effort = "effort"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).effort ==\
                   jsondetails.effort, 'After set effort'
            jsondetails.self_paced = True
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).self_paced ==\
                   jsondetails.self_paced
            jsondetails.start_date = datetime.datetime(2010, 10, 1, 0, tzinfo=UTC)
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).start_date ==\
                   jsondetails.start_date
            jsondetails.end_date = datetime.datetime(2011, 10, 1, 0, tzinfo=UTC)
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).end_date ==\
                   jsondetails.end_date
            jsondetails.certificate_available_date = datetime.datetime(2010, 10, 1, 0, tzinfo=UTC)
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user)\
                .certificate_available_date == jsondetails.certificate_available_date
            jsondetails.course_image_name = "an_image.jpg"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).course_image_name ==\
                   jsondetails.course_image_name
            jsondetails.banner_image_name = "an_image.jpg"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).banner_image_name ==\
                   jsondetails.banner_image_name
            jsondetails.video_thumbnail_image_name = "an_image.jpg"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user)\
                .video_thumbnail_image_name == jsondetails.video_thumbnail_image_name
            jsondetails.language = "hr"
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).language ==\
                   jsondetails.language
            jsondetails.learning_info = ["test", "test"]
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).learning_info ==\
                   jsondetails.learning_info

            jsondetails.instructor_info = {
                "instructors": [
                    {
                        "name": "test",
                        "title": "test",
                        "organization": "test",
                        "image": "test",
                        "bio": "test"
                    }
                ]
            }
            assert CourseDetails.update_from_json(self.course.id, jsondetails.__dict__, self.user).instructor_info ==\
                   jsondetails.instructor_info

    def test_toggle_pacing_during_course_run(self):
        self.course.start = datetime.datetime.now(UTC)
        self.store.update_item(self.course, self.user.id)

        details = CourseDetails.fetch(self.course.id)
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            updated_details = CourseDetails.update_from_json(
                self.course.id,
                dict(details.__dict__, self_paced=True),
                self.user
            )
        assert not updated_details.self_paced

    @ddt.data(*ABOUT_ATTRIBUTES)
    def test_fetch_about_attribute(self, attribute_name):
        attribute_value = 'test_value'
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            CourseDetails.update_about_item(self.course, attribute_name, attribute_value, self.user.id)
        assert CourseDetails.fetch_about_attribute(self.course.id, attribute_name) == attribute_value

    def test_fetch_about_attribute_error(self):
        attribute_name = 'not_an_about_attribute'
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            CourseDetails.update_about_item(self.course, attribute_name, 'test_value', self.user.id)
        with pytest.raises(ValueError):
            CourseDetails.fetch_about_attribute(self.course.id, attribute_name)

    def test_fetch_video(self):
        video_value = 'dQw4w9WgXcQ'
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            CourseDetails.update_about_video(self.course, video_value, self.user.id)
        assert CourseDetails.fetch_youtube_video_id(self.course.id) == video_value
        video_url = CourseDetails.fetch_video_url(self.course.id)
        self.assertRegex(video_url, fr'http://.*{video_value}')

    def test_update_about_video_normalises_and_rejects(self):
        with self.store.branch_setting(ModuleStoreEnum.Branch.draft_preferred, self.course.id):
            CourseDetails.update_about_video(self.course, f'https://youtu.be/{VIDEO_ID}?si=x', self.user.id)
            assert CourseDetails.fetch_youtube_video_id(self.course.id) == VIDEO_ID
            with pytest.raises(ValidationError):
                CourseDetails.update_about_video(self.course, 'junk', self.user.id)
            assert CourseDetails.fetch_youtube_video_id(self.course.id) == VIDEO_ID

    @ddt.data(
        (
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.END,
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.END_WITH_DATE
        ),
        (
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.END_WITH_DATE,
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.END_WITH_DATE
        ),
        (
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.EARLY_NO_INFO,
            None,
            CertificatesDisplayBehaviors.EARLY_NO_INFO
        ),
        (
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            "invalid_option",
            EXAMPLE_CERTIFICATE_AVAILABLE_DATE,
            CertificatesDisplayBehaviors.END_WITH_DATE
        ),
        (
            None,
            CertificatesDisplayBehaviors.END,
            None,
            CertificatesDisplayBehaviors.END
        ),
        (
            None,
            CertificatesDisplayBehaviors.END_WITH_DATE,
            None,
            CertificatesDisplayBehaviors.END
        ),
        (
            None,
            CertificatesDisplayBehaviors.EARLY_NO_INFO,
            None,
            CertificatesDisplayBehaviors.EARLY_NO_INFO
        ),
        (
            None,
            "invalid_option",
            None,
            CertificatesDisplayBehaviors.END
        ),
    )
    @ddt.unpack
    def test_validate_certificate_settings(self, stored_date, stored_behavior, expected_date, expected_behavior):
        assert CourseDetails.validate_certificate_settings(
            stored_date, stored_behavior
        ) == (expected_date, expected_behavior)
