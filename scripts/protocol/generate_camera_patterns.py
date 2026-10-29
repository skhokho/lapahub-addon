"""Replace only ephemeral official-demo camera assets with synthetic patterns.

Run before starting the scratch HA instance; never use on a home HA filesystem.
"""
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw

assert Path('/.dockerenv').exists()
assert Path('/config/configuration.yaml').read_text().startswith('homeassistant:\n  name: Isolated protocol fixture')
root = Path('/usr/src/homeassistant/homeassistant/components/demo')
patterns = []
for index in range(4):
    image = Image.new('RGB', (160, 96), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    for y in range(0, 96, 16):
        for x in range(0, 160, 16):
            if (x // 16 + y // 16 + index) % 2:
                draw.rectangle((x, y, x + 15, y + 15), fill=(50 + index * 50, 220 - index * 40, 50 + index * 30))
    draw.rectangle((index * 32, 0, index * 32 + 15, 95), fill=(240, 40, 40))
    for extension in ('jpg', 'png'):
        path = root / f'demo_{index}.{extension}'
        image.save(path)
        patterns.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size})
print(json.dumps(patterns))
