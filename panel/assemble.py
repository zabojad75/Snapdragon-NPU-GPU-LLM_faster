#!/usr/bin/env python3
"""Assemble the final panel/index.html from build/step1.css + step2.html + step3-5.js."""
import pathlib

here = pathlib.Path(__file__).resolve().parent
b = here / "build"

css = (b / "step1.css").read_text()
html = (b / "step2.html").read_text()
js = "\n".join((b / f).read_text() for f in ("step3.js", "step4.js", "step5.js"))

page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llmnpu Panel</title>
<style>
{css}
</style>
</head>
<body>
{html}
<script>
{js}
</script>
</body>
</html>
"""
(here / "index.html").write_text(page)
(b / "combined.js").write_text(js)
print(f"assembled {here / 'index.html'} ({len(page)} chars, JS {js.count(chr(10))+1} lines)")
