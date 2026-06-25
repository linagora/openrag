import React from 'react'
import { createRoot } from 'react-dom/client'

const container = document.getElementById('root')
if (!container) throw new Error('#root not found')
createRoot(container).render(<div>chat-openrag boot OK</div>)
