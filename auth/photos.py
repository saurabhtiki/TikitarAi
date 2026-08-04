import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from auth.exceptions import PhotoProcessingError

logger = logging.getLogger(__name__)

PHOTOS_DIR = Path("data") / "photos"
PHOTO_SIZE = 256


def save_user_photo(user_id: int, uploaded_file) -> str:
    """Crops an uploaded image to a centered square, resizes it, and saves it as
    data/photos/user_{user_id}.png, overwriting any previous photo for that user.

    Returns the saved path as a string.

    Raises:
        PhotoProcessingError: if the uploaded file isn't a readable image.
    """
    try:
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        image = Image.open(uploaded_file).convert("RGB")

        width, height = image.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize((PHOTO_SIZE, PHOTO_SIZE))

        photo_path = PHOTOS_DIR / f"user_{user_id}.png"
        image.save(photo_path, format="PNG")
    except (UnidentifiedImageError, OSError) as error:
        logger.exception("Failed to process uploaded photo for user_id %s.", user_id)
        raise PhotoProcessingError("That file couldn't be read as an image. Please try a different photo.") from error

    return str(photo_path.resolve())
