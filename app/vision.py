"""Frame transforms and Gemini detection parsing."""
import json
import re


ZOOM_MIN = 1.0
_OBJECT_NAME = re.compile(r"^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+){0,7}$")
_INSTRUCTION_WORDS = {
    "act", "answer", "assistant", "developer", "disregard", "execute", "forget",
    "follow", "ignore", "instruction", "instructions", "output", "pretend",
    "prompt", "respond", "return", "roleplay", "run", "say", "show", "system",
    "tell", "user", "write",
}


def validate_object_name(value):
    """Accept only a short object-name phrase, never free-form instructions."""
    value = " ".join(value.split())
    if not value:
        return ""
    if len(value) > 64 or not _OBJECT_NAME.fullmatch(value):
        raise ValueError("Enter an object name using letters, numbers, spaces, or hyphens (up to 64 characters).")
    words = {word.lower() for word in value.replace("-", " ").split()}
    if words & _INSTRUCTION_WORDS:
        raise ValueError("Enter an object name only; instruction-like text is not accepted.")
    return value


def digital_zoom(frame, zoom, cv2):
    """Center-crop and resize a frame for software zoom."""
    if zoom <= ZOOM_MIN:
        return frame
    height, width = frame.shape[:2]
    crop_width = max(1, round(width / zoom))
    crop_height = max(1, round(height / zoom))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    crop = frame[top:top + crop_height, left:left + crop_width]
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_LINEAR)


def parse_detections(response_text, width, height):
    """Convert Gemini JSON boxes into pixel boxes and normalized centers."""
    text = response_text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    detections = json.loads(text)
    if not isinstance(detections, list):
        raise ValueError("Gemini response must be a JSON list.")

    boxes = []
    for item in detections:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            continue
        coords = item.get("box_2d")
        if not isinstance(coords, list) or len(coords) != 4:
            continue
        try:
            ymin, xmin, ymax, xmax = map(float, coords)
        except (TypeError, ValueError):
            continue
        x1 = max(0, min(width - 1, round(xmin * width / 1000)))
        y1 = max(0, min(height - 1, round(ymin * height / 1000)))
        x2 = max(0, min(width - 1, round(xmax * width / 1000)))
        y2 = max(0, min(height - 1, round(ymax * height / 1000)))
        if x2 > x1 and y2 > y1:
            boxes.append({
                "label": item["label"].strip(),
                "box": (x1, y1, x2, y2),
                "center": ((x1 + x2) / (2 * width), (y1 + y2) / (2 * height)),
            })
    return boxes
