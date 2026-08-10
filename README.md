# The Daily Duck — Version 3.1 FIXED

Version 3.1 removes the runtime JSON `fetch()` dependency.

## Why
The site is a static Vercel site. Version 3 loaded the daily content using
`fetch('data/today.json')` and `fetch('data/archive.json')`.

Version 3.1 loads one plain JavaScript data file before the main script:

`data/content.js`

This makes the daily content available immediately as
`window.DAILY_DUCK_DATA`, so the title, story, image and Archive can render
without an asynchronous JSON request.

## Daily update — only 2 things are normally needed

1. Add today's image:
   `assets/ducks/YYYY-MM-DD-name.png`

2. Replace/update:
   `data/content.js`

`data/content.js` contains:
- `today`
- `archive`

So the current duck and Archive are updated together.

## Usually do not change
- index.html
- styles.css
- script.js
- favicon.svg

## First deployment of Version 3.1
Upload the full contents of this ZIP to the GitHub repository root and commit
to `main`. Vercel should deploy automatically.

The existing JSON files remain in `/data` as reference/backward compatibility,
but the live Version 3.1 page does not depend on them.
