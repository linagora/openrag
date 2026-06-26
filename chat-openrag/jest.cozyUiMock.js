// Stub for cozy-ui (transpiled path and root) — jest only.
// All components are replaced with pass-through fragments.
const React = require('react')
// Pass-through fragment. Several cozy-ui components (Button, Chip, …) render
// their text via a `label` prop rather than children, so surface both.
const Comp = ({ children, label }) =>
  React.createElement(React.Fragment, null, label != null ? label : null, children || null)
// A few cozy-ui named exports are hooks, not components — return real values so
// consumers (e.g. cozy-search's Sidebar via useBreakpoints) don't blow up.
const hooks = {
  useBreakpoints: () => ({ isMobile: false, isTablet: false, isDesktop: true })
}
module.exports = new Proxy({ default: Comp, __esModule: true }, {
  get (target, key) {
    if (key === '__esModule' || key === 'default') return target[key]
    if (key in hooks) return hooks[key]
    return Comp
  }
})
