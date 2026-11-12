"""Empacota só o add-on, sem capturas, checkpoints ou modelos pesados."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
output = root / 'dist' / 'sketchfab_link_v2-2.5.3.zip'
output.parent.mkdir(exist_ok=True)
with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
    for path in sorted((root / 'sketchfab_link_v2').rglob('*')):
        if path.is_file() and (path.suffix in ('.py', '.js') or path.name == 'LICENSE') and '__pycache__' not in path.parts:
            archive.write(path, path.relative_to(root))
print(output)
