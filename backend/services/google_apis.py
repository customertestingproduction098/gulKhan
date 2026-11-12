"""AWS Rekognition person detection (replaces Google Vision).

Firebase push still uses google.auth in services/alert_system.py.
"""

from services.aws_rekognition import (
    AWSRekognitionService,
    get_rekognition_service,
    get_vision_service,
)

GoogleVisionService = AWSRekognitionService
get_google_vision_service = get_vision_service

__all__ = [
    "AWSRekognitionService",
    "GoogleVisionService",
    "get_vision_service",
    "get_rekognition_service",
    "get_google_vision_service",
]

