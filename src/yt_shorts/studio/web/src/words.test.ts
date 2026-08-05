import { describe, expect, it } from 'vitest'
import { findWordProblems, insertWordAfter, removeWord, wordsEqual } from './words'

const w = (start: number, end: number, text: string) => ({ start, end, text })

describe('wordsEqual', () => {
  it('is true for identical lists', () => {
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 1, ' a')])).toBe(true)
  })
  it('is true for two empty lists', () => {
    expect(wordsEqual([], [])).toBe(true)
  })
  it('is false when a length differs', () => {
    expect(wordsEqual([w(0, 1, ' a')], [])).toBe(false)
  })
  it('is false when the text differs', () => {
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 1, ' b')])).toBe(false)
  })
  it('is false when a timestamp differs', () => {
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 2, ' a')])).toBe(false)
  })
})

describe('wordsEqual with decoder words', () => {
  // What these two cases pin, precisely: wordsEqual stays SENSITIVE to a
  // leading space, so a word list is "unchanged" only if its raw text is
  // byte-identical to what the server sent. That is the invariant the rest of
  // the editor relies on - WordsEditor trims only what it DISPLAYS and keeps
  // the raw text in state, because trimming into state would make every word
  // differ from its saved form and every clip would open showing "Unsaved
  // changes".
  //
  // What they do NOT do, measured rather than assumed (a review broke both and
  // watched these stay green): catch that state-level trim. They call
  // wordsEqual on hand-built arrays and never render WordsEditor, never invoke
  // its onChange and never exercise the load path, so a trim introduced in any
  // of those three places is invisible here. The guard for THAT is the
  // Playwright E2E - tests/test_studio_e2e.py's
  // test_a_hand_typed_word_keeps_its_boundary, which asserts the field's
  // displayed value AND that a freshly loaded clip is not dirty.
  it('treats a freshly loaded word list as unchanged', () => {
    const loaded = [w(0, 1, ' drives'), w(1, 2, " it's")]
    expect(wordsEqual(loaded, loaded.map((word) => ({ ...word })))).toBe(true)
  })

  it('sees a difference when only the leading space differs', () => {
    expect(wordsEqual([w(0, 1, ' Rei')], [w(0, 1, 'Rei')])).toBe(false)
  })
})

describe('insertWordAfter', () => {
  it('splits the target row in half and leaves the rest alone', () => {
    // Whisper does not leave a GAP where words are missing - it stretches the
    // last word it recognised across them. Measured on the operator's clip:
    // "that's" spanning 4.92 -> 12.36, 7.5 seconds on one word. So an insert
    // takes its time from the row it follows, not from empty space.
    const words = [w(1.36, 4.92, ' and'), w(4.92, 12.36, " that's"), w(12.36, 12.6, ' all')]
    expect(insertWordAfter(words, 1)).toEqual([
      w(1.36, 4.92, ' and'),
      w(4.92, 8.64, " that's"),
      w(8.64, 12.36, ''),
      w(12.36, 12.6, ' all'),
    ])
  })

  it('rounds the midpoint to two decimals', () => {
    // The inputs render with decimalScale={2}. An unrounded 8.639999999999999
    // would DISPLAY as 8.64 while being stored as something else, so a value
    // the operator never touched would read back differently from what they
    // see.
    const [, inserted] = insertWordAfter([w(0, 0.7, 'x')], 0)
    expect(inserted.start).toBe(0.35)
    expect(insertWordAfter([w(0, 1 / 3, 'x')], 0)[1].start).toBe(0.17)
  })

  it('gives the new row empty text, which renders nothing', () => {
    // captions.group_words skips empty words and normalise_word_boundaries
    // leaves them alone, so adding a row before deciding what goes in it
    // changes no output.
    expect(insertWordAfter([w(0, 2, 'x')], 0)[1].text).toBe('')
  })

  it('works after the last row', () => {
    expect(insertWordAfter([w(0, 1, 'a'), w(1, 3, 'b')], 1)).toEqual([
      w(0, 1, 'a'), w(1, 2, 'b'), w(2, 3, ''),
    ])
  })

  it('seeds a first word into an empty transcript', () => {
    // Decision 8: an empty transcript must not be a dead end with no row to
    // insert after. The index is ignored here, which is what lets the
    // component call this one function for both cases.
    expect(insertWordAfter([], 0)).toEqual([w(0, 1, '')])
    expect(insertWordAfter([], -1)).toEqual([w(0, 1, '')])
  })

  it('changes nothing for an out-of-range index', () => {
    const words = [w(0, 1, 'a')]
    expect(insertWordAfter(words, 5)).toEqual(words)
    expect(insertWordAfter(words, -1)).toEqual(words)
  })

  it('does not mutate its argument', () => {
    const words = [w(0, 2, 'a')]
    insertWordAfter(words, 0)
    expect(words).toEqual([w(0, 2, 'a')])
  })

  it('never produces a row findWordProblems would flag', () => {
    // The pairing the whole design rests on, and the one nothing asserted
    // until a review broke it: the tool must not hand the operator a red
    // border on a row the tool itself just created.
    //
    // Rounding did exactly that. A row from 1.0111 to 1.0161 has a true
    // midpoint of 1.0136, which rounds to 1.01 - BELOW its own start - so the
    // first half ended before it began. These timings are realistic, not
    // contrived: transcribe.py writes faster-whisper's floats through
    // unrounded, and short rows are precisely the ones this feature exists to
    // split. 1.008 -> 1.009 covers the mirror case, where the midpoint rounds
    // UP past the row's own end.
    const spans = [
      [1.001, 1.002], [1.0111, 1.0161], [1.008, 1.009],
      [4.92, 12.36], [0, 0.7], [2.5, 2.5],
    ]
    for (const [start, end] of spans) {
      const result = insertWordAfter([w(start, end, 'x')], 0)
      expect(findWordProblems(result), `splitting ${start} -> ${end}`).toEqual([[], []])
    }
  })
})

describe('removeWord', () => {
  it('removes the row at the index', () => {
    expect(removeWord([w(0, 1, 'a'), w(1, 2, 'b'), w(2, 3, 'c')], 1)).toEqual([
      w(0, 1, 'a'), w(2, 3, 'c'),
    ])
  })

  it('can empty the list', () => {
    // A legitimate state meaning "this clip has no captions", not an error.
    expect(removeWord([w(0, 1, 'a')], 0)).toEqual([])
  })

  it('changes nothing for an out-of-range index', () => {
    // THREE elements, not one. With a single-element fixture this test cannot
    // fail: slice's clamping happens to reproduce the unchanged list whether
    // or not the bounds guard exists, so deleting the guard left the suite
    // green. On three elements a missing guard duplicates rows instead
    // ([a,b,a,b,c] for index -1), which is the defect this pins.
    const words = [w(0, 1, 'a'), w(1, 2, 'b'), w(2, 3, 'c')]
    expect(removeWord(words, 5)).toEqual(words)
    expect(removeWord(words, -1)).toEqual(words)
  })

  it('does not mutate its argument', () => {
    const words = [w(0, 1, 'a'), w(1, 2, 'b')]
    removeWord(words, 0)
    expect(words).toHaveLength(2)
  })
})

describe('findWordProblems', () => {
  it('treats contiguous decoder output as clean', () => {
    // THE case that matters most: faster-whisper emits words[i].end ===
    // words[i+1].start. Flagging that would put a red border on every row of
    // every clip and make the warning worthless.
    expect(findWordProblems([w(0, 1.5, 'a'), w(1.5, 2, 'b'), w(2, 3, 'c')])).toEqual([[], [], []])
  })

  it('flags an overlap on the later row', () => {
    // The later row is the one whose start is wrong, and the one the operator
    // just typed into.
    expect(findWordProblems([w(0, 2, 'a'), w(1.5, 3, 'b')])).toEqual([[], ['overlap']])
  })

  it('flags a row whose end precedes its start', () => {
    expect(findWordProblems([w(5, 4, 'a')])).toEqual([['inverted']])
  })

  it('can report both problems on one row', () => {
    expect(findWordProblems([w(0, 3, 'a'), w(2, 1, 'b')])).toEqual([[], ['overlap', 'inverted']])
  })

  it('treats a zero-length row as clean', () => {
    // Reachable by splitting a row shorter than 0.01s (the rounding case in
    // insertWordAfter). Harmless: it is neither an overlap nor an inversion,
    // and it renders nothing.
    expect(findWordProblems([w(1, 1, 'a')])).toEqual([[]])
  })

  it('is clean for an empty list and a single word', () => {
    expect(findWordProblems([])).toEqual([])
    expect(findWordProblems([w(0, 1, 'a')])).toEqual([[]])
  })
})

describe('wordsEqual after a structural change', () => {
  it('sees an added row', () => {
    // The Save button is gated on `dirty`, which is computed with wordsEqual.
    // If length were not compared, adding a row would leave Save disabled and
    // the whole feature would be unreachable.
    const words = [w(0, 2, 'a')]
    expect(wordsEqual(words, insertWordAfter(words, 0))).toBe(false)
  })

  it('sees a removed row', () => {
    const words = [w(0, 1, 'a'), w(1, 2, 'b')]
    expect(wordsEqual(words, removeWord(words, 1))).toBe(false)
  })
})
