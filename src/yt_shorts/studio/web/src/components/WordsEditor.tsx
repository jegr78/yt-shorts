import { ActionIcon, Alert, Button, Group, NumberInput, ScrollArea, Stack, Table, TextInput, Text } from '@mantine/core'
import type { Word } from '../api'
import { findWordProblems, insertWordAfter, removeWord } from '../words'

interface WordsEditorProps {
  words: Word[]
  onChange: (words: Word[]) => void
  onJumpTo: (time: number) => void
  /** Freezes the editable cells (start/end/text) while a render runs - the
   * jump control stays live, it only moves the preview and writes nothing. */
  disabled?: boolean
}

/**
 * The transcript as editable rows - the operator's actual job in this
 * tool: notice a misheard word and fix its text (timestamps are editable
 * too, for the rarer case a boundary is wrong, but text is the common
 * case). Edits here are staged in the parent's local state; nothing is
 * sent to the server until the explicit Save action (see ClipEditor).
 */
export function WordsEditor({ words, onChange, onJumpTo, disabled = false }: WordsEditorProps) {
  function updateWord(index: number, patch: Partial<Word>) {
    const next = words.slice()
    next[index] = { ...next[index], ...patch }
    onChange(next)
  }

  const problems = findWordProblems(words)
  const flagged = problems.filter((row) => row.length > 0).length

  if (words.length === 0) {
    return (
      <Stack gap="xs" align="flex-start">
        <Text size="sm" c="dimmed">
          No transcript words for this clip.
        </Text>
        <Button
          size="xs"
          variant="light"
          color="steel"
          disabled={disabled}
          onClick={() => onChange(insertWordAfter(words, 0))}
        >
          Add word
        </Button>
      </Stack>
    )
  }

  return (
    <Stack gap="xs">
      {flagged > 0 && (
        <Alert color="orange" variant="light" p="xs">
          <Text size="xs">
            {flagged === 1 ? '1 row has' : `${flagged} rows have`} a timing that
            overlaps the previous word or ends before it starts. You can still
            save - but a clip whose words OVERLAP renders with no subtitles at
            all, so fix the highlighted rows before rendering.
          </Text>
        </Alert>
      )}
      <ScrollArea.Autosize mah={320} offsetScrollbars>
        <Table stickyHeader verticalSpacing={4}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th w={84}>
                <Text size="xs" fw={600} tt="uppercase" c="dimmed">
                  Start
                </Text>
              </Table.Th>
              <Table.Th w={84}>
                <Text size="xs" fw={600} tt="uppercase" c="dimmed">
                  End
                </Text>
              </Table.Th>
              <Table.Th>
                <Text size="xs" fw={600} tt="uppercase" c="dimmed">
                  Text
                </Text>
              </Table.Th>
              <Table.Th w={110} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {words.map((word, index) => (
              // eslint-disable-next-line react/no-array-index-key
              <Table.Tr key={index}>
                <Table.Td>
                  <NumberInput
                    size="xs"
                    value={word.start}
                    onChange={(value) => updateWord(index, { start: Number(value) || 0 })}
                    step={0.1}
                    decimalScale={2}
                    hideControls
                    disabled={disabled}
                    error={problems[index].includes('overlap')}
                    className="tnum"
                    styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
                  />
                </Table.Td>
                <Table.Td>
                  <NumberInput
                    size="xs"
                    value={word.end}
                    onChange={(value) => updateWord(index, { end: Number(value) || 0 })}
                    step={0.1}
                    decimalScale={2}
                    hideControls
                    disabled={disabled}
                    error={problems[index].includes('inverted')}
                    className="tnum"
                    styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
                  />
                </Table.Td>
                <Table.Td>
                  <TextInput
                    size="xs"
                    value={word.text.trim()}
                    onChange={(event) => updateWord(index, { text: event.currentTarget.value })}
                    disabled={disabled}
                  />
                </Table.Td>
                <Table.Td>
                  <Group gap={0} justify="center" wrap="nowrap">
                    <ActionIcon
                      variant="subtle"
                      color="steel"
                      size="sm"
                      title={`Jump preview to ${word.start.toFixed(1)}s`}
                      onClick={() => onJumpTo(word.start)}
                    >
                      ▶
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="steel"
                      size="sm"
                      title="Insert a word after this one (splits its time in half)"
                      disabled={disabled}
                      onClick={() => onChange(insertWordAfter(words, index))}
                    >
                      +
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="steel"
                      size="sm"
                      title="Remove this word"
                      disabled={disabled}
                      onClick={() => onChange(removeWord(words, index))}
                    >
                      ✕
                    </ActionIcon>
                  </Group>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </ScrollArea.Autosize>
    </Stack>
  )
}
