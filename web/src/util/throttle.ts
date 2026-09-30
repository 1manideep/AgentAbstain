/**
 * Trailing throttle: `fn` runs at most once per `ms`, and a call made during the
 * quiet window is guaranteed to run at the end of it (nothing is dropped).
 */
export function throttle(fn: () => void, ms: number): { call: () => void; flush: () => void; cancel: () => void } {
  let last = -Infinity
  let timer: ReturnType<typeof setTimeout> | null = null
  const run = () => {
    timer = null
    last = performance.now()
    fn()
  }
  return {
    call() {
      if (timer !== null) return
      const wait = ms - (performance.now() - last)
      if (wait <= 0) run()
      else timer = setTimeout(run, wait)
    },
    flush() {
      if (timer !== null) {
        clearTimeout(timer)
        run()
      }
    },
    cancel() {
      if (timer !== null) clearTimeout(timer)
      timer = null
    },
  }
}
