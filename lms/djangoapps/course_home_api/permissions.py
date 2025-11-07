"""
Permissions for the course home apis and associated actions
"""
from bridgekeeper import perms

from lms.djangoapps.courseware.rules import HasAccessRule

CAN_MASQUERADE_LEARNER_PROGRESS = 'course_home_api.can_masquerade_progress'

perms[CAN_MASQUERADE_LEARNER_PROGRESS] = HasAccessRule('staff')
