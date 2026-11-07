// Claude Code mod: a band above the prompt with the decision count and one button per mode.
const SKILL = 'what-did-agent-do:what-did-agent-do'
const MODES = [
  ['explain', 'Explain', '1'],
  ['backfill', 'Backfill', '2'],
  ['interview', 'Interview', '3'],
]

export const countDecisions = text => ({
  total: (text.match(/^## D-\d+/gm) ?? []).length,
  created: (text.match(/^- \*\*Origin:\*\* new\b/gm) ?? []).length,
})

let counts = { total: 0, created: 0 }

async function refresh($) {
  const root = await $.session.root()
  const text = await $.fs.read(`${root}/docs/decisions.md`).catch(() => '')
  counts = countDecisions(text)
  $.ui.invalidate('ui.render')
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await refresh($).catch(() => {})
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($).catch(() => {})
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // Other mods share the band: keep what they draw below ours.
    const below = await next(e).catch(() => undefined)
    const { Box, Text, Button } = $.ui.resolve(e)
    const row = Box({
      flexDirection: 'row',
      columnGap: 2,
      children: [
        Text({ dimColor: true, children: ['what-did-agent-do'] }),
        Text({ children: [`${counts.total} decisions · ${counts.created} new`] }),
        ...MODES.map(([mode, label, hotkey]) =>
          Button({ key: mode, label, hotkey, plain: true, onPress: () => $.command.run({ command: SKILL, args: mode }) }),
        ),
      ],
    })
    return below ? Box({ flexDirection: 'column', children: [row, below] }) : row
  })
}
