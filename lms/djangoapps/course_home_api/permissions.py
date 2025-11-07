"""
Permissions for the course home apis and associated actions
"""
from bridgekeeper import perms
from lms.djangoapps.courseware.rules import HasAccessRule


CAN_MASQUARADE_LEARNER_PROGRESS = 'course_home_api.can_masquarade_progress'

perms[CAN_MASQUARADE_LEARNER_PROGRESS] = HasAccessRule('staff')
