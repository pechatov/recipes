from pathlib import Path

from django.core.exceptions import ValidationError


MAX_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def validate_recipe_image(image):
    if image.size > MAX_IMAGE_SIZE:
        raise ValidationError("Изображение должно быть не больше 10 МБ.")
    extension = Path(image.name).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError("Поддерживаются JPG, PNG и WebP.")
