/* AbujaCine Admin JS */
document.addEventListener('DOMContentLoaded', async () => {
  const r = await api('/api/me');
  if (!r.is_admin) { window.location.href = '/'; return; }
  tab('dashboard', document.querySelector('.slink'));
});

async function tab(name, btn) {
  document.querySelectorAll('.slink').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.apanel').forEach(p => p.classList.remove('active'));
  const el = document.getElementById('panel-' + name);
  if (el) el.classList.add('active');
  if (name === 'dashboard')  loadDash();
  if (name === 'movies')     loadMovies();
  if (name === 'showtimes')  loadShowtimes();
  if (name === 'bookings')   loadBookings();
}

async function loadDash() {
  const s = await api('/api/admin/stats');
  document.getElementById('stats-row').innerHTML = `
    <div class="stat-card"><div class="s-icon">👥</div><div class="s-lbl">Users</div><div class="s-val">${s.total_users}</div></div>
    <div class="stat-card"><div class="s-icon">🎬</div><div class="s-lbl">Movies</div><div class="s-val">${s.total_movies}</div></div>
    <div class="stat-card"><div class="s-icon">🎟</div><div class="s-lbl">Confirmed</div><div class="s-val">${s.total_bookings}</div></div>
    <div class="stat-card"><div class="s-icon">₦</div><div class="s-lbl">Revenue</div><div class="s-val">${Number(s.total_revenue).toLocaleString()}</div></div>`;
  document.getElementById('popular-list').innerHTML = s.popular_movies.map((m,i) => `
    <div class="pop-item">
      <span class="pop-rank ${i<3?'top':'}'}">#${i+1}</span>
      <span class="pop-title">${esc(m.title)}</span>
      <span class="pop-count">${m.bookings} bookings</span>
    </div>`).join('') || '<p style="color:var(--text2)">No data yet</p>';
}

async function loadMovies() {
  const movies = await api('/api/admin/movies');
  const wrap = document.getElementById('movies-tbl');
  if (!movies.length) { wrap.innerHTML='<p>No movies</p>'; return; }
  wrap.innerHTML = `<table class="admin-table">
    <thead><tr><th>Title</th><th>Genre</th><th>Rating</th><th>Status</th><th>Actions</th></tr></thead>
    <tbody>${movies.map(m => `<tr>
      <td><strong>${esc(m.title)}</strong></td>
      <td>${esc(m.genre)}</td><td>★ ${m.rating}</td>
      <td><span class="badge ${m.is_active?'badge-green':'badge-red'}">${m.is_active?'Active':'Hidden'}</span></td>
      <td>
        <button class="tbl-btn" onclick="toggleMovie(${m.id},${m.is_active})">${m.is_active?'Hide':'Show'}</button>
        <button class="tbl-btn del" onclick="deleteMovie(${m.id})">Delete</button>
      </td>
    </tr>`).join('')}</tbody></table>`;
}

async function toggleMovie(id, active) {
  const all = await api('/api/admin/movies');
  const m   = all.find(x => x.id === id);
  if (!m) return;
  await api(`/api/admin/movies/${id}`, { method:'PUT', body:{...m, is_active: active?0:1} });
  loadMovies();
}
async function deleteMovie(id) {
  if (!confirm('Deactivate this movie?')) return;
  await api(`/api/admin/movies/${id}`, {method:'DELETE'});
  loadMovies(); toast('Movie deactivated','info');
}
async function addMovie() {
  const errEl = document.getElementById('mf-err'); errEl.classList.add('hidden');
  const data = {
    title: val('m-title'), genre: val('m-genre'), description: val('m-desc'),
    duration_min: +val('m-dur'), rating: +val('m-rat'),
    poster_url: val('m-poster'), director: val('m-dir'),
    cast_list: val('m-cast'), release_year: +val('m-yr')
  };
  if (!data.title) { showErr(errEl,'Title required'); return; }
  const r = await api('/api/admin/movies', {method:'POST', body:data});
  if (r.error) { showErr(errEl, r.error); return; }
  closeModal('add-movie-modal'); loadMovies(); toast('Movie added!','success');
}

async function loadShowtimes() {
  const rows = await api('/api/admin/showtimes');
  const wrap = document.getElementById('showtimes-tbl');
  if (!rows.length) { wrap.innerHTML='<p>No showtimes</p>'; return; }
  wrap.innerHTML = `<table class="admin-table">
    <thead><tr><th>Movie</th><th>Cinema</th><th>Hall</th><th>Date & Time</th><th>Price</th></tr></thead>
    <tbody>${rows.map(s=>`<tr>
      <td><strong>${esc(s.title)}</strong></td>
      <td>${esc(s.cinema_name)}</td><td>${esc(s.hall_name)}</td>
      <td>${new Date(s.showtime).toLocaleString()}</td>
      <td>₦${Number(s.price).toLocaleString()}</td>
    </tr>`).join('')}</tbody></table>`;
}
async function prepShowtime() {
  const [movies, halls] = await Promise.all([api('/api/movies'), api('/api/halls')]);
  document.getElementById('st-movie').innerHTML = movies.map(m=>`<option value="${m.id}">${esc(m.title)}</option>`).join('');
  document.getElementById('st-hall').innerHTML  = halls.map(h=>`<option value="${h.id}">${esc(h.cinema_name)} — ${esc(h.name)} (${h.total_rows*h.total_cols} seats)</option>`).join('');
  openModal('add-showtime-modal');
}
async function addShowtime() {
  const data = {
    movie_id: +val('st-movie'), hall_id: +val('st-hall'),
    showtime: val('st-time').replace('T',' ')+':00', price: +val('st-price')
  };
  const r = await api('/api/admin/showtimes', {method:'POST', body:data});
  if (r.error) { toast(r.error,'error'); return; }
  closeModal('add-showtime-modal'); loadShowtimes(); toast('Showtime + seats created!','success');
}

async function loadBookings() {
  const rows = await api('/api/admin/bookings');
  const wrap = document.getElementById('bookings-tbl');
  if (!rows.length) { wrap.innerHTML='<p>No bookings</p>'; return; }
  wrap.innerHTML = `<table class="admin-table">
    <thead><tr><th>User</th><th>Movie</th><th>Cinema</th><th>Showtime</th><th>Seat</th><th>Amount</th><th>Status</th></tr></thead>
    <tbody>${rows.map(b=>`<tr>
      <td>${esc(b.username)}</td><td>${esc(b.title)}</td>
      <td>${esc(b.cinema_name)}</td>
      <td>${new Date(b.showtime).toLocaleString()}</td>
      <td>${b.row_label}${b.seat_number}</td>
      <td>₦${Number(b.amount).toLocaleString()}</td>
      <td><span class="badge ${b.status==='confirmed'?'badge-green':'badge-rose'}">${b.status}</span></td>
    </tr>`).join('')}</tbody></table>`;
}

function openModal(id) { document.getElementById(id).classList.remove('hidden') }
function closeModal(id) { document.getElementById(id).classList.add('hidden') }
function closeOverlay(e,id) { if(e.target===e.currentTarget) closeModal(id) }
function val(id) { return (document.getElementById(id)||{}).value?.trim()||'' }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }
function showErr(el,msg) { el.textContent=msg; el.classList.remove('hidden') }
function toast(msg,type='info') {
  const el = document.createElement('div');
  el.className=`toast ${type}`;
  el.textContent=msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(()=>el.remove(),3500);
}
async function api(url,opts={}) {
  const {method='GET',body}=opts;
  try {
    const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
    return await r.json();
  } catch(e){return{error:'Network error'}}
}
