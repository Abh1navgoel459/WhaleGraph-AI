const runBtn = document.getElementById('run');
const riskEl = document.getElementById('risk');

runBtn.addEventListener('click', async () => {
  riskEl.textContent = 'Running...';
  const token = document.getElementById('token').value.trim();
  const scenario = document.getElementById('scenario').value;
  const batchSize = parseInt(document.getElementById('batch').value, 10);

  const res = await fetch('/api/replay', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, scenario, batch_size: batchSize })
  });

  const data = await res.json();
  riskEl.textContent = JSON.stringify(data, null, 2);
});
