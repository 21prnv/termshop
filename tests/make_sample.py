"""Generate a colorful test photo (gradient + shapes) as sample.jpg."""
from PIL import Image, ImageDraw

W, H = 640, 480
img = Image.new("RGB", (W, H))
px = img.load()
for y in range(H):
    for x in range(W):
        px[x, y] = (int(255 * x / W), int(255 * y / H), int(255 * (1 - x / W)))
d = ImageDraw.Draw(img)
d.ellipse((120, 90, 360, 330), fill=(250, 210, 60), outline=(30, 30, 30), width=6)
d.rectangle((400, 260, 600, 430), fill=(60, 170, 90), outline=(255, 255, 255), width=4)
d.polygon([(80, 430), (200, 250), (320, 430)], fill=(200, 60, 60))
img.save("sample.jpg", quality=92)
print("sample.jpg", img.size)
