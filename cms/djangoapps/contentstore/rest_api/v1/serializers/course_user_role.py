"""
API Serializer for the current user's course role.
"""

from rest_framework import serializers


class CourseUserRoleSerializer(serializers.Serializer):
    """
    Serializer for the current user's role in a given course.
    """
    role = serializers.CharField(allow_null=True)
