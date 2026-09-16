/** `Node.ELEMENT_NODE`, spelled out so the predicate stays usable for a plain
 *  object stand-in and never depends on a live `Node` binding. */
const ELEMENT_NODE = 1

/** True when this node is a field that consumes printable keystrokes itself, so
 *  a global single-key hotkey must not claim them. `SELECT` counts: a printable
 *  key there is option typeahead, not a spare keystroke. */
export function isEditableElement(node: EventTarget | null | undefined): boolean {
  const el = node as HTMLElement | null
  if (!el || el.nodeType !== ELEMENT_NODE || typeof el.tagName !== 'string') return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  return el.isContentEditable === true
}

/** The minimum an event has to offer to be classified. Widened from `Event` so
 *  a caller can classify a synthetic event object. */
export interface EditableTargetEvent {
  target: EventTarget | null
  composedPath?: () => EventTarget[]
}

/**
 * The editable element a keyboard event came from, or `null` when the keystroke
 * is free for a global hotkey to claim.
 *
 * Reads the event's COMPOSED PATH rather than `event.target`. The code editor's
 * editable node is a `contentEditable` element inside an OPEN shadow root, and a
 * composed event crossing that boundary is retargeted: by the time a
 * document-level listener runs, `event.target` — and `document.activeElement`
 * with it — report the shadow HOST, a plain `<div>` that answers `false` to
 * every editability question. So a guard reading either one lets the keystroke
 * through and the caret loses the character.
 *
 * `composedPath()[0]` is the node that actually holds the caret; the entries
 * after it are its ancestors across every shadow boundary, which is what makes
 * an INHERITED `contentEditable` visible here too. `event.target` is the
 * fallback for an event object that carries no path.
 *
 * Note for tests: jsdom does not retarget composed shadow events, so a
 * document-level listener there still sees the inner node and a
 * `target`-only guard appears to work. Only a real engine shows the bug.
 */
export function editableEventTarget(e: EditableTargetEvent): HTMLElement | null {
  const path = typeof e.composedPath === 'function' ? e.composedPath() : []
  if (path.length === 0) return isEditableElement(e.target) ? (e.target as HTMLElement) : null
  for (const node of path) {
    if (isEditableElement(node)) return node as HTMLElement
  }
  return null
}

/** True when a keyboard event originates inside an editable field. See
 *  {@link editableEventTarget} for why this reads the composed path. */
export function isEditableTarget(e: EditableTargetEvent): boolean {
  return editableEventTarget(e) !== null
}
