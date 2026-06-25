module.exports = new Proxy({}, { get: (_t, key) => String(key) })
