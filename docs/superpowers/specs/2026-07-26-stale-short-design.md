# The studio serves a stale rendered short

## Why

An operator re-renders a clip and the studio keeps playing the previous
version. Only a hard browser reload shows the new one, which is a bad trade:
it throws away the whole page's state to refresh one `<video>` element.

The cause is two independent layers, and the second is why a soft refresh
cannot help.

### Layer A: the player has no way to notice the file changed

`api.ts`'s `shortUrl(name)` returns a constant path, and the clip payload's
only statement about the short is `has_short: true` (`api.py`'s `_summary`).
So after a re-render the refetched payload is byte-identical, React sees the
same `src` string, never touches the DOM attribute, and the mounted `<video>`
keeps the resource it already loaded.

There is proof this is the dominant layer: `App.tsx` ALREADY refetches the
open clip when a render job finishes, commented "so that shows up without the
operator reloading the page". That machinery works. It updates everything
except the one string the video element keys on.

### Layer B: the response invites the browser to keep the old bytes

`get_short` returns a bare `FileResponse`. Measured against a live TestClient:

```
cache-control: None          <- nothing at all
etag: "781f8ec821e749d36add2a3f3db78d7e"
last-modified: Sun, 26 Jul 2026 11:59:28 GMT
accept-ranges: bytes

If-None-Match with that exact etag  -> 200, full body (not 304)
If-Modified-Since with its own date -> 200, full body
```

With no `Cache-Control`, the browser falls back to heuristic freshness and may
serve the cached copy **without making a request at all** — that is the
stubbornness, and why only a hard reload (which bypasses the cache) works. And
when it does revalidate, the route ignores the conditional headers and re-sends
the entire video.

### The consumer that makes this more than cosmetic

`shortUrl` has two callers, not one. `ManualUploadPanel` uses it as a
`download` link, because a `manual` channel cannot be API-uploaded and the
operator uploads the file by hand in YouTube Studio. A stale cache there means
downloading the OLD video and publishing it — a silently wrong artifact on the
channel, not a stale preview. Both callers are fixed by versioning the one URL
builder they share.

## Decisions

1. **The server states the short's version; the client puts it in the URL.**
   A token derived from the file's own `stat()` — `st_mtime_ns` and `st_size` —
   is exposed as `short_version` beside `has_short`, and the client appends it
   as `?v=<token>`. The `src` then changes exactly when the bytes change, no
   matter who re-rendered: the studio, a CLI run, another tab, a later session.
   That last point is the requirement. The operator's own workflow is a CLI
   render, where the studio's job-completion effect never fires at all, so a
   client-side `?t=Date.now()` bumped on job completion — the cheaper fix —
   would not fix the reported bug.

2. **`(mtime_ns, size)`, not a content hash.** Hashing a multi-megabyte video
   on every clip-list request is O(size) per clip, and a list holds dozens.
   `stat()` is one syscall, the same class as the `.exists()` it replaces. It
   is also exactly the identity Starlette's `FileResponse` already derives its
   own ETag from, so this reuses the server's existing notion of file identity
   rather than inventing a second, disagreeing one.

3. **The token is opaque.** The client passes it through and never parses it.
   Its shape (`"<mtime_ns>-<size>"`) is an implementation detail of one
   function; a test may pin that it CHANGES, and must not pin its format.

4. **A stale `?v=` still serves the current file.** The parameter is a cache
   key, not a precondition. Refusing a mismatched token would turn a bookmarked
   link, or a request already in flight when a render lands, into a 404 — a new
   failure mode invented to protect against nothing. `absent`, `stale` and
   `garbage` all serve the same bytes; only the cache policy differs (5).

5. **Cache policy follows from the versioning, and `immutable` is never a
   lie.** The response is cached hard — `private, max-age=31536000, immutable`,
   which keeps scrubbing and seeking fast — only when the requested `v` MATCHES
   the file's current token. Every other case (absent, stale, garbage) gets
   `private, no-cache`: revalidate before use. `private` is the honest statement
   for per-operator workspace content.

   The match check is what makes the hard policy truthful, and it costs nothing
   because the token is already computed to answer the request. Without it there
   is a real window: a client asks for `?v=OLD` just after a render lands, and
   we would be telling it to cache the NEW bytes under the OLD key for a year.
   Harmless in practice — the next payload carries the new token, so the stale
   key is never requested again — but a URL advertised as immutable would be
   serving content that had changed, and the next person to reason about this
   code deserves better than that.

   The non-matching branches are a safety net, not a hot path: the player only
   renders when `has_short` is true, and `short_version` is non-null whenever it
   is, so the shipped UI always sends a matching token. The other cases stay
   reachable by hand — curl, an old bookmark — and must stay correct.

6. **A refetch on focus, because a CLI render has nothing else to trigger
   one.** `App.tsx` refreshes the clip list and the open clip — the same pair of
   calls the existing job-completion effect makes — on BOTH the window's `focus`
   event and `document`'s `visibilitychange` (refetching when
   `document.visibilityState === 'visible'`). Both, because neither covers the
   case alone: switching browser TABS reliably fires `visibilitychange`, while
   alt-tabbing to another APPLICATION does not do so dependably across platforms
   and browser versions, and `focus` is what carries that case. A double
   refetch when both fire is two small idempotent GETs, which is a better
   trade than picking one event and missing half the situations.

   Alt-tab from the terminal back to the studio and the new short is there. No
   polling: a timer per open editor, forever, for a file that changes rarely, is
   a worse trade than one event handler.

   **This is safe for unsaved edits, and not by luck.** `ClipEditor` resets its
   staged `localTitle`/`localWords`/`localWindow` only when `clip.name` changes,
   deliberately — its own comment says "not on every prop update, or an
   in-progress correction would be wiped out by an unrelated status change
   bumping the same clip object". A focus refetch of the SAME clip therefore
   replaces the `clip` prop without touching staged edits, and `PreviewPane`
   (keyed on the clip name) does not remount either. The only visible effect is
   the one we want: `short_version` changes, so the player's `src` changes.

7. **No 304 handling.** With `immutable` on every URL the UI produces,
   revalidation is rare by construction, and the one path where a 304 would pay
   off is the unversioned branch the UI never takes. Adding it would also mean
   either depending on a Starlette internal ETag format or introducing a second
   one. Left out deliberately, not overlooked.

## What changes

**Server** (`src/yt_shorts/studio/api.py`):
- a small helper returning the token or `None` for an absent short, and
  `has_short` derived from the same single `stat()` rather than a separate
  `.exists()` call
- `short_version` emitted wherever `has_short` is, so both the clip list and
  the clip detail carry it
- `get_short` accepts an optional `v` query parameter, reads it ONLY to choose
  between the two `Cache-Control` values, and serves the same file either way

**Client** (`src/yt_shorts/studio/web/src/`):
- `shortUrl(name, version?)` appends `?v=` when given a version
- `ClipSummary`/clip detail types gain `short_version: string | null`
- both call sites pass it: the `<video>` in `ClipEditor` and the download link
  in `ManualUploadPanel`
- one effect in `App.tsx` refetching list + open clip on focus/visibility

**No change** to how renders are started, to `clipstore`, or to any other
media: the preview image and the brand preview are fetched into blob object
URLs minted per response, so they cannot go stale this way.

## One free property worth knowing

The fix needs no cache-clearing on upgrade. The URL the client requests after
the change (`…/short?v=…`) is a different cache key from the one the browser
has an old entry for (`…/short`), so the very first load after the upgrade is
already fresh.

## Testing

- **The token tracks the file.** Absent short → `None`. Two calls with no
  change → identical. Bytes replaced → different. Asserted against a real
  file on disk, and pinning that it CHANGED rather than what it equals.
- **Both payloads carry it.** The clip list and the clip detail each expose
  `short_version`, non-null exactly when `has_short` is true.
- **All four cache branches.** A request whose `v` matches the current token
  returns the `immutable` policy; absent, stale and garbage tokens each return
  `no-cache`. All four return the same bytes — decision (4) is a test, not a
  comment — and the stale case specifically must NOT be a 404.
- **The client builds both forms.** Vitest on `shortUrl` with and without a
  version, including a token that needs URL encoding.
- **The E2E guard, which is the only thing that can catch layer A.** Open a
  clip with a rendered short, record the player's `src` and the download
  link's `href`, replace the short's bytes on disk, trigger the focus path,
  and assert BOTH changed and both carry the new token the API now reports.
  Asserting only "src is non-empty", or comparing something minted per
  response, would pass against the bug — this project has shipped exactly that
  twice (see CLAUDE.md's note on the blob-URL guard that could never fail).
- The six pinned overlay hashes and `captions.py` are untouched by all of
  this; nothing here renders.

## Out of scope

- 304/conditional-request handling (decision 7).
- Content-hash versioning (decision 2).
- Polling the open clip, or any push/websocket notification of a finished CLI
  render. Focus is the trigger.
- Making the CLI notify a running studio. The studio's job runner and the CLI
  deliberately do not know about each other; the only thing they share is the
  `EventLock` and the files on disk.
- Any change to the preview routes, which do not have this defect.
