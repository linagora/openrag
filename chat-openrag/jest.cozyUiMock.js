// Stub for cozy-ui (transpiled path and root) — jest only.
// All components are replaced with pass-through fragments.
const React = require('react')
const Comp = ({ children }) => React.createElement(React.Fragment, null, children || null)
module.exports = new Proxy({ default: Comp, __esModule: true }, {
  get (target, key) {
    if (key === '__esModule' || key === 'default') return target[key]
    return Comp
  }
})
