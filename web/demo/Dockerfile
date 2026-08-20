FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml termshop.py README.md LICENSE ./
COPY tests/make_sample.py tests/sample.jpg ./seed/
RUN pip install --no-cache-dir ".[web]"
# a few sample photos for visitors to play with
RUN mkdir /photos && python - << 'PY'
from PIL import Image, ImageEnhance, ImageOps
img = Image.open("seed/sample.jpg").convert("RGB")
img.save("/photos/gradient.jpg", quality=92)
ImageOps.mirror(ImageEnhance.Color(img).enhance(0.35)).save("/photos/faded.jpg", quality=92)
img.rotate(180).save("/photos/flipped.jpg", quality=92)
PY
EXPOSE 8000
ENV PUBLIC_URL=""
CMD termshop-web /photos --host 0.0.0.0 --port 8000 ${PUBLIC_URL:+--public-url "$PUBLIC_URL"}
