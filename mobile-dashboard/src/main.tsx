import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles/theme.css';
import './styles/layout.css';
import './styles/setup-card.css';
import './styles/regime.css';
import './styles/killswitch.css';

const container = document.getElementById('root');
if (container === null) {
  throw new Error('#root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
