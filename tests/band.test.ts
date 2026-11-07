import { expect, test } from 'claude-code/testing'

const LOG = '# Decisions\n\n## D-001: A\n- **Origin:** new\n\n## D-002: B\n- **Origin:** follows src/a.ts\n'
const BAND = {
  plugin: 'what-did-agent-do',
  component: 'AbovePrompt',
  surface: 'terminal',
  viewport: { columns: 100, rows: 30 },
  props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 100, scroll: { offset: 0, bodyRows: 4 }, view: {} },
} as const

test('band counts the log and runs the chosen mode', async ($, on) => {
  const ran: unknown[] = []
  on('session.start', () => ({ cwd: '/work' }))
  on('session.root', () => ({ value: '/work' }))
  on('fs.read', ($, e) => ({ value: e.path.endsWith('docs/decisions.md') ? LOG : '' }))
  on('command.run', ($, e) => {
    ran.push({ command: e.command, args: e.args })
    return { value: {} }
  })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other mods'] }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: '2 decisions · 1 new' })).toBeDefined()
  await ui.press({ key: 'interview' })
  expect(ran).toEqual([{ command: 'what-did-agent-do:what-did-agent-do', args: 'interview' }])
})
