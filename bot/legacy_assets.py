"""Возвращает загруженные ранее картинки из постоянного хранилища Bothost."""
import os
from pathlib import Path
import shutil


def restore_legacy_assets():
    root = Path(__file__).resolve().parents[1]
    data = Path(os.getenv('DATA_DIR', '/app/data'))
    source = data / 'legacy_images'
    target = root / 'bot' / 'images'
    count = 0
    if source.is_dir():
        for path in source.rglob('*'):
            if path.is_file() and not path.is_symlink():
                destination = target / path.relative_to(source)
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    count += 1
    if count:
        print(f'Restored persistent legacy images: {count}')
