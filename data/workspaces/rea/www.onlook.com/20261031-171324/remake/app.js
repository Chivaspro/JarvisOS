// Client-side interactions and API integration matching observed network endpoints

document.addEventListener('DOMContentLoaded', () => {
  // 1. Fetch Session from Local/Mock Backend API
  fetchSession();

  // 2. Fetch GitHub Repository stats dynamically
  fetchRepoStats();

  // 3. Interactive Sandbox Studio Setup
  setupStudioControls();

  // 4. Clipboard helper for npx command
  setupCopyCommand();
});

// Auth Session simulation
async function fetchSession() {
  try {
    const res = await fetch('/api/auth/get-session');
    if (res.ok) {
      const data = await res.json();
      const authBadge = document.getElementById('auth-status');
      const userDisplay = document.getElementById('user-display');
      if (authBadge && userDisplay && data.session) {
        userDisplay.textContent = data.session.user ? data.session.user.name : 'Guest';
        authBadge.classList.remove('hidden');
      }
    }
  } catch (err) {
    // Graceful offline fallback
    console.debug('Session check skipped, using standard client state.');
  }
}

// GitHub API Integration matching observed endpoint
async function fetchRepoStats() {
  try {
    const res = await fetch('/repos/onlook-dev/onlook');
    if (res.ok) {
      const data = await res.json();
      if (data.stargazers_count) {
        const starK = (data.stargazers_count / 1000).toFixed(1) + 'k';
        const starBadge = document.getElementById('gh-star-count');
        const statStars = document.getElementById('stat-stars');
        if (starBadge) starBadge.textContent = `★ ${starK}`;
        if (statStars) statStars.textContent = `${data.stargazers_count.toLocaleString()}`;
      }
      if (data.forks_count) {
        const statForks = document.getElementById('stat-forks');
        if (statForks) statForks.textContent = `${data.forks_count.toLocaleString()}`;
      }
    }
  } catch (err) {
    console.debug('Using cached stats.');
  }
}

// Visual Studio Sandbox Interactive State
function setupStudioControls() {
  const container = document.getElementById('editable-container');
  const heading = document.getElementById('card-heading');
  const desc = document.getElementById('card-desc');
  const colorPicker = document.getElementById('bg-color-picker');
  const colorVal = document.getElementById('bg-color-val');
  const radiusSlider = document.getElementById('radius-slider');
  const radiusVal = document.getElementById('radius-val');
  const paddingSlider = document.getElementById('padding-slider');
  const paddingVal = document.getElementById('padding-val');
  const tailwindPreview = document.getElementById('tailwind-preview');
  
  // Toolbar mode buttons
  const toolSelect = document.getElementById('tool-select');
  const toolText = document.getElementById('tool-text');
  const toolPreview = document.getElementById('tool-preview');
  const demoActionBtn = document.getElementById('demo-action-btn');
  const clickFeedback = document.getElementById('click-feedback');
  let clickCount = 0;

  function updateTailwindOutput() {
    const radius = radiusSlider.value;
    const padding = paddingSlider.value;
    const hex = colorPicker.value;
    tailwindPreview.textContent = `rounded-[${radius}px] p-[${padding}px] bg-[${hex}]/20 border border-white/10`;
  }

  // Radius Slider
  radiusSlider.addEventListener('input', (e) => {
    const val = e.target.value;
    container.style.borderRadius = `${val}px`;
    radiusVal.textContent = `${val}px`;
    updateTailwindOutput();
  });

  // Padding Slider
  paddingSlider.addEventListener('input', (e) => {
    const val = e.target.value;
    container.style.padding = `${val}px`;
    paddingVal.textContent = `${val}px`;
    updateTailwindOutput();
  });

  // Color Accent Picker
  colorPicker.addEventListener('input', (e) => {
    const val = e.target.value;
    colorVal.textContent = val;
    demoActionBtn.style.backgroundColor = val;
    updateTailwindOutput();
  });

  // Interactive Live Button inside container
  demoActionBtn.addEventListener('click', () => {
    clickCount++;
    clickFeedback.textContent = `Clicks: ${clickCount}`;
    clickFeedback.classList.add('text-indigo-400');
    setTimeout(() => clickFeedback.classList.remove('text-indigo-400'), 250);
  });

  // Switch modes
  toolSelect.addEventListener('click', () => {
    setToolActive(toolSelect);
    heading.contentEditable = "false";
    desc.contentEditable = "false";
    document.getElementById('selection-box').style.display = 'block';
  });

  toolText.addEventListener('click', () => {
    setToolActive(toolText);
    heading.contentEditable = "true";
    desc.contentEditable = "true";
    heading.focus();
  });

  toolPreview.addEventListener('click', () => {
    setToolActive(toolPreview);
    heading.contentEditable = "false";
    desc.contentEditable = "false";
    document.getElementById('selection-box').style.display = 'none';
  });

  function setToolActive(btn) {
    [toolSelect, toolText, toolPreview].forEach(b => b.classList.remove('active', 'bg-white/10', 'text-white'));
    btn.classList.add('active', 'bg-white/10', 'text-white');
  }

  // Simulated AI Inspector Prompt
  const aiBtn = document.getElementById('ai-generate-btn');
  const aiInput = document.getElementById('ai-prompt-input');
  const aiMsg = document.getElementById('ai-msg');

  aiBtn.addEventListener('click', () => {
    const val = aiInput.value.trim().toLowerCase();
    if (!val) return;
    
    aiMsg.textContent = 'Applying changes to DOM...';
    aiMsg.classList.add('text-indigo-400');

    setTimeout(() => {
      if (val.includes('glass') || val.includes('transparent')) {
        container.style.backgroundColor = 'rgba(255, 255, 255, 0.05)';
        container.style.backdropFilter = 'blur(16px)';
        aiMsg.textContent = 'Applied glassmorphism theme';
      } else if (val.includes('minimal') || val.includes('flat')) {
        container.style.backgroundColor = '#0b0c0e';
        container.style.border = '1px solid rgba(255,255,255,0.05)';
        aiMsg.textContent = 'Applied minimal theme';
      } else {
        container.style.borderColor = 'rgba(99, 102, 241, 0.4)';
        aiMsg.textContent = `Applied styles for "${val}"`;
      }
      updateTailwindOutput();
    }, 400);
  });
}

function setupCopyCommand() {
  const btn = document.getElementById('copy-npx-btn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    navigator.clipboard.writeText('npx onlook@latest');
    const original = btn.innerHTML;
    btn.innerHTML = `<span class="text-emerald-400">✓</span> Copied to clipboard!`;
    setTimeout(() => {
      btn.innerHTML = original;
    }, 2000);
  });
}
```

```text
