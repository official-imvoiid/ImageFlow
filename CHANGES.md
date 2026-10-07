# What changed in ImageFlow 2.0

Same features, same look, same shortcuts. The code was reorganised into a
small package and rewritten for speed and robustness.

## Speed

| Area | Before | Now |
|---|---|---|
| Scroll | every wheel tick deleted and rebuilt all canvas items and re-created every visible `PhotoImage` | virtualised grid, items updated in place; ~1–2 ms per tick with 3 000 images |
| Hover / click | linear scan over every image position on each mouse move | per-column binary search, ~7 µs |
| Thumbnails | plain FIFO queue, full decode per width, dropped requests when the queue filled, redecoded on every resize or column change | visible-first priority queue, master thumbnail decoded once (JPEG draft mode), column widths derived from it, stale prefetch dropped, 1 GB disk cache so folders reopen instantly |
| Opening a folder | blocked until 800 image headers were read sequentially (seconds on a slow disk or under antivirus) | paints after the directory listing; at most 0.15 s spent on the first screen's ratios, visible tiles read first, the rest stream in with the view anchored, and ratios are remembered per folder so the next open needs no file reads |
| Single view | full image re-decoded/resized on every zoom step and on every pan mouse-move | thumbnail shown instantly, real image decoded on a worker at the size needed, neighbours pre-decoded, only the visible crop is rendered when zoomed, panning moves the existing frame |
| Sidebar | full tree rebuilt or every row rewritten on each selection change | rows addressed by index, only changed rows touched |
| Search | relayout on every keystroke | debounced |

## Fits the device

- Every pixel dimension (toolbar, sidebar, paddings, list rows, outlines)
  scales with the display DPI, so 125 %–200 % Windows displays and HiDPI
  Linux screens no longer clip buttons or crop list rows. `--scale` overrides.
- The window opens at 84 % of the screen, centred, and remembers its size
  and position (and theme, columns, sidebar width) between runs.
- The sidebar has a drag handle; the toolbar wraps to two rows when the
  window is narrow instead of overlapping.
- Zoom controls moved to the bottom bar with Previous/Next, grouped so they
  read "Zoom − 100% +". When the bar is narrow the counter, then the "Zoom"
  caption, are dropped and the status text is ellipsised, so nothing is ever
  half-clipped.
- Mouse-wheel scrolling in the grid is animated (eased) like a native gallery.
- Fewer thumbnail threads on small machines and a slower idle tick, so the
  app stays light while a folder loads; slightly damaged JPEGs are shown
  instead of marked broken; the web poll fetches only selected ids.
- Web view: on screens under 900 px the sidebar is a slide-in panel, labels
  shorten, thumbnails fill the width; touch devices get tap-to-open,
  long-press to select, swipe to navigate, pinch to zoom, tap to hide chrome.

## Scrolling far in a large folder

- Thumbnails are requested only once the view pauses, and requests for
  tiles you already scrolled past are dropped, so a long flick no longer
  queues every screen it passed and the screen you stop on fills first.
- Tiles whose ratio is not yet known are laid out 4:3 instead of square,
  with flat placeholders and a clear mark for unreadable files.

## Web file list

- Only one row is highlighted at a time; earlier rows no longer keep their
  highlight after each click.
- The virtual list refreshes when the panel opens, resizes or is rebuilt, so
  it never shows a handful of rows until the next scroll.

## Web view is view-only

- A permanent red notice says the page is for viewing. Selection changes are
  refused by the server and the page unless started with `--web-edit`.

## Also

- Focus-mode arrows and the exit cross no longer sit in a solid box; they
  are drawn directly over the image with a thin shadow, in the window and
  on the page.

- Leaving the single view through a filter change (deselecting the image
  you are viewing in Selected Only, a search that empties the list, opening
  another folder) now removes the image; it used to stay drawn over the
  empty grid. A search while viewing keeps the same image even though its
  position in the list changes. Both in the window and the page.
- Web page: keyboard shortcuts work again after clicking a radio button or
  checkbox (they were treated as a text field being typed in).
- The port box accepts any port; a browser-blocked one (6000) is moved to
  the next usable port and the reason stays in the status line.

- Port box in the toolbar (default 6001); a busy port falls through to the
  next free one and the box shows which. Remembered between runs.
- The red ring that marks a clicked image is thicker, has a white inner edge,
  blinks when it appears and stays longer, in both the window and the page.
- Panel button: the sidebar pane check compared names to window objects on
  some Tk builds, so the button did nothing. Fixed.

## Bugs fixed

- Mouse wheel on Linux: `event.delta` exists but is 0 for Button-4/5, so the
  grid did not scroll and zoom only went out. Fixed.
- Importing a list: only names *with* extensions matched, so the default
  export (without extensions) could not be re-imported. Now matches with or
  without extension, case-insensitively, with full paths, commas, quotes, BOM.
- EXIF orientation was ignored (phone photos sideways). Honoured everywhere.
- Typing `s` or space in the search box while in single view toggled
  selection. Keyboard shortcuts now ignore text fields.
- Tooltip forced a geometry flush (`update_idletasks`) on every mouse move.
- Worker exceptions were swallowed with bare `except:`; a failing image is now
  marked broken once instead of being retried forever.
- `natsort` missing crashed at import; now falls back to a built-in natural key.
- 16-bit and palette images rendered wrong or failed; normalised safely.
- Opening a folder while a previous scan ran was ignored; now supersedes it.

## New ways to use it

- **Import** button (toolbar) and `--txt` accept lists with or without
  extensions.
- **Web** button / `--serve` serves the gallery on `http://localhost:6001`
  for browsing and selecting, sharing the selection live with the window
  (list export/import remain desktop-only). `--serve --no-gui` runs without
  tkinter at all.
- `Serve.bat` / `Serve.sh` launchers; `Start.*` pass arguments through.

## Layout

```
ImageFlow.pyw, imageflow/{library,thumbs,gui,web,cli}.py, imageflow/static/index.html, tests/
```
