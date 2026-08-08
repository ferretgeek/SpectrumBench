const root = document.documentElement;
const palette = document.getElementById('palette');
const theme = document.getElementById('theme');
const themeMeta = document.querySelector('meta[name="theme-color"]');
const lightColors = {iris: '#f4f6ff', jade: '#f1f7f4', sunrise: '#fff8f1'};

function applyTheme(color) {
  root.dataset.color = color;
  const dark = color === 'dark';
  theme.textContent = dark ? '浅色' : '暗色';
  theme.setAttribute('aria-label', dark ? '切换为浅色模式' : '切换为暗色模式');
  themeMeta.content = dark ? '#17191d' : lightColors[palette.value];
}

palette.value = localStorage.getItem('spectrumbench-demo-palette') || 'iris';
root.dataset.palette = palette.value;
applyTheme(localStorage.getItem('spectrumbench-demo-color') || 'light');

palette.addEventListener('change', () => {
  root.dataset.palette = palette.value;
  localStorage.setItem('spectrumbench-demo-palette', palette.value);
  applyTheme(root.dataset.color);
});
theme.addEventListener('click', () => {
  const next = root.dataset.color === 'dark' ? 'light' : 'dark';
  localStorage.setItem('spectrumbench-demo-color', next);
  applyTheme(next);
});

const modes = {
  uncached: {text: '等长随机标记主动击穿前缀缓存，观察刻意昂贵的理论边界。', speed: '41.6', ttft: '1.48', cache: '0%'},
  daily: {text: '六轮真实结对编程对话，观察上下文增长与自然缓存。', speed: '54.8', ttft: '1.21', cache: '68%'},
  codex: {text: '稳定复用长代码上下文与缓存键，观察高缓存连续任务。', speed: '61.3', ttft: '1.05', cache: '94%'},
};
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-mode]').forEach(item => item.classList.toggle('active', item === button));
  const data = modes[button.dataset.mode];
  document.getElementById('methodText').textContent = data.text;
  document.getElementById('speed').textContent = data.speed;
  document.getElementById('ttft').textContent = data.ttft;
  document.getElementById('cache').textContent = data.cache;
}));
