// Stub for @linagora/twake-icons — jest only; not installed in this app.
const React = require('react')
const Icon = ({ icon, ...props }) => React.createElement('span', props)
module.exports = new Proxy({ Icon }, {
  get (target, key) {
    return key in target ? target[key] : key
  }
})
