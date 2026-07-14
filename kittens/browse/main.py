#!/usr/bin/env python
# License: GPLv3. Docs-only shim: the implementation is Go (main.go).

help_text = (
    'Browse the web inside the terminal. Renders Chrome into the'
    ' kitty/kilix pane: page pixels stream at full resolution via the'
    ' graphics protocol while page text is drawn as real, selectable'
    ' terminal glyphs. Requires google-chrome or chromium on PATH.'
)
usage = '[url]'

if __name__ == '__main__':
    raise SystemExit('This should be run as kitten browse')
elif __name__ == '__doc__':
    import sys
    cd = sys.cli_docs  # type: ignore
    cd['usage'] = usage
    cd['help_text'] = help_text
    cd['short_desc'] = 'Browse the web inside the terminal'
