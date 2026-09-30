/** Word-level diff (LCS) between two summaries, for the self-version history. */
export interface DiffToken {
  kind: 'same' | 'add' | 'del'
  text: string
}

export function wordDiff(a: string, b: string): DiffToken[] {
  const A = a.split(/\s+/).filter(Boolean)
  const B = b.split(/\s+/).filter(Boolean)
  const n = A.length
  const m = B.length
  // LCS table (summaries are ≤ ~60 words, so O(n·m) is trivial)
  const dp: Uint16Array[] = []
  for (let i = 0; i <= n; i++) dp.push(new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i]![j] = A[i] === B[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!)
    }
  }
  const out: DiffToken[] = []
  let i = 0
  let j = 0
  const push = (kind: DiffToken['kind'], text: string) => {
    const last = out[out.length - 1]
    if (last && last.kind === kind) last.text += ' ' + text
    else out.push({ kind, text })
  }
  while (i < n && j < m) {
    if (A[i] === B[j]) {
      push('same', A[i]!)
      i++
      j++
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) {
      push('del', A[i]!)
      i++
    } else {
      push('add', B[j]!)
      j++
    }
  }
  while (i < n) push('del', A[i++]!)
  while (j < m) push('add', B[j++]!)
  return out
}
