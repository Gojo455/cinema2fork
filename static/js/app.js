/* ═══════════════════════════════════════════════════════════════════════════
   AbujaCine — Frontend App JS
   Real Paystack inline popup · Seat-aware recommendations · Live seat map
   ═══════════════════════════════════════════════════════════════════════════ */

// ─── STATE ─────────────────────────────────────────────────────────────────
const S = {
  user: null,
  movies: [],
  showtimeId: null,
  selectedSeat: null,
  lockExpiry: null,
  booking: null,
  seatPoll: null,
  lockTick: null,
};

// ─── BOOT ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  applyTheme(localStorage.getItem('theme') || 'light');
  await checkAuth();
  loadMovies();
  loadGenres();
  loadCinemas();
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAllModals(); });
});

// ─── AUTH ──────────────────────────────────────────────────────────────────
async function checkAuth() {
  const r = await api('/api/me');
  if (r.logged_in) setUser(r);
}

function setUser(u) {
  S.user = u;
  document.getElementById('nav-auth').style.display = 'none';
  document.getElementById('nav-user').style.display = 'flex';
  document.getElementById('user-chip').textContent  = u.username;
  show('rec-nav'); show('tix-nav'); show('hero-rec-btn');
}

async function login() {
  const username = val('login-user'), password = val('login-pass');
  const errEl = ge('login-err'); hide(errEl);
  if (!username || !password) { showErr(errEl, 'Please fill in all fields'); return; }
  const r = await api('/api/login', { method: 'POST', body: { username, password } });
  if (r.error) { showErr(errEl, r.error); return; }
  setUser(r); closeModal('login-modal');
  toast('Welcome back, ' + r.username + ' ✦', 'success');
  if (r.is_admin) window.location.href = '/admin';
}

async function register() {
  const username = val('reg-user'), email = val('reg-email'), password = val('reg-pass');
  const errEl = ge('reg-err'); hide(errEl);
  if (!username || !email || !password) { showErr(errEl, 'All fields required'); return; }
  const r = await api('/api/register', { method: 'POST', body: { username, email, password } });
  if (r.error) { showErr(errEl, r.error); return; }
  setUser(r); closeModal('register-modal');
  toast('Account created! Welcome ✦', 'success');
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  S.user = null;
  document.getElementById('nav-auth').style.display = 'flex';
  document.getElementById('nav-user').style.display  = 'none';
  hide('rec-nav'); hide('tix-nav'); hide('hero-rec-btn');
  showSection('browse');
  toast('Signed out', 'info');
}

// ─── NAV / SECTIONS ────────────────────────────────────────────────────────
function showSection(name) {
  document.querySelector('.main').scrollIntoView({ behavior: 'smooth' });
  document.querySelectorAll('.section').forEach(s => s.classList.add('hidden'));
  const sec = ge('section-' + name);
  if (!sec) return;
  sec.classList.remove('hidden');
  if (name === 'recommendations') {
    if (!S.user) { openModal('login-modal'); return; }
    loadRecs();
  } else if (name === 'my-bookings') {
    if (!S.user) { openModal('login-modal'); return; }
    loadTickets();
  }
}

// ─── MOVIES ────────────────────────────────────────────────────────────────
async function loadMovies() {
  const movies = await api('/api/movies');
  S.movies = movies;
  renderGrid(movies, 'movies-grid', false);
}

async function loadGenres() {
  const genres = await api('/api/genres');
  const sel = ge('genre-select');
  genres.forEach(g => {
    const o = document.createElement('option');
    o.value = g; o.textContent = g; sel.appendChild(o);
  });
}

function filterMovies() {
  const q = val('search-input').toLowerCase();
  const g = val('genre-select');
  const filtered = S.movies.filter(m =>
    (!g || m.genre === g) &&
    (!q || m.title.toLowerCase().includes(q) || (m.description||'').toLowerCase().includes(q)));
  renderGrid(filtered, 'movies-grid', false);
}

function renderGrid(movies, containerId, isRec) {
  const grid = ge(containerId);
  if (!movies.length) {
    grid.innerHTML = `<div class="empty-state"><div class="ei">🎬</div><h3>No films found</h3><p>Try adjusting your filters</p></div>`;
    return;
  }
  grid.innerHTML = movies.map((m, i) => {
    const mid   = m.movie_id || m.id;
    const score = m.score != null ? Math.round(m.score * 100) : null;
    const poster = m.poster_url
      ? `<img class="movie-poster" src="${esc(m.poster_url)}" alt="${esc(m.title)}" loading="lazy"
             onerror="this.outerHTML='<div class=\\'poster-ph\\'>🎬</div>'">`
      : `<div class="poster-ph">🎬</div>`;

    const badge = isRec && score != null ? `<div class="rec-score-badge">${score}%</div>` : '';

    const pills = isRec ? `
      <div class="seat-info-pill">
        ${m.available_seats!=null ? `<span class="mini-pill mp-sage">⬛ ${m.available_seats} seats</span>` : ''}
        ${m.best_quality    ? `<span class="mini-pill mp-gold">⭐ ${m.best_quality}/10</span>` : ''}
        ${m.pref_match!=null? `<span class="mini-pill mp-rose">🎯 ${m.pref_match}% match</span>` : ''}
      </div>
      ${m.showtime ? `<div style="font-size:0.7rem;color:var(--text3);margin-top:0.35rem">${fmtDateShort(m.showtime)} · ${esc(m.cinema_name||m.hall_name||'')}</div>` : ''}` : '';

    return `<div class="movie-card" style="animation-delay:${i*0.04}s" onclick="openMovieDetail(${mid})">
      ${poster}${badge}
      <div class="movie-body">
        <div class="movie-title">${esc(m.title)}</div>
        <div class="movie-meta">
          <span class="genre-tag">${esc(m.genre)}</span>
          <span class="rating-tag">${m.rating}</span>
        </div>
        ${pills}
      </div>
    </div>`;
  }).join('');
}

// ─── RECOMMENDATIONS ───────────────────────────────────────────────────────
async function loadRecs() {
  ge('recs-grid').innerHTML = '<div class="shimmer"></div>';
  const [recs, prefs] = await Promise.all([api('/api/recommendations'), api('/api/my-preferences')]);

  // Update preference summary card
  const pc = ge('pref-card');
  if (prefs && prefs.seat_position_pref) {
    const gw = JSON.parse(prefs.genre_weights || '{}');
    const topGenre = Object.entries(gw).sort((a,b)=>b[1]-a[1])[0];
    pc.innerHTML = `
      Prefers <strong>${prefs.seat_position_pref} ${prefs.seat_zone_pref}</strong> seats ·
      Avg quality taste: <strong>${prefs.avg_quality_pref}/10</strong><br>
      ${topGenre ? `Favourite genre: <strong>${topGenre[0]}</strong>` : 'No bookings yet — watch more!'}
      · ${prefs.booking_count} booking${prefs.booking_count !== 1 ? 's' : ''}`;
  } else {
    pc.innerHTML = 'Book your first ticket to train your personal recommendations ✦';
  }

  if (!recs.length) {
    ge('recs-grid').innerHTML = `<div class="empty-state"><div class="ei">✦</div><h3>No recommendations yet</h3><p>Book a film to unlock personalised picks</p></div>`;
    return;
  }
  renderGrid(recs, 'recs-grid', true);
}

// ─── MOVIE DETAIL ──────────────────────────────────────────────────────────
async function openMovieDetail(mid) {
  openModal('movie-modal');
  ge('movie-detail').innerHTML = '<div class="shimmer" style="height:320px"></div>';
  const { movie: m, showtimes } = await api(`/api/movies/${mid}`);

  // Group showtimes by cinema
  const byCinema = {};
  showtimes.forEach(s => {
    const cn = s.cinema_name || s.hall_name;
    if (!byCinema[cn]) byCinema[cn] = [];
    byCinema[cn].push(s);
  });

  const stHtml = Object.entries(byCinema).map(([cinema, sts]) => `
    <div style="margin-bottom:1rem">
      <div style="font-size:0.78rem;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.5rem">${esc(cinema)}</div>
      <div style="display:flex;flex-wrap:wrap;gap:0.55rem">
        ${sts.map(s => {
          const avail = s.available_seats || 0;
          const full  = avail === 0;
          return `<button class="st-btn ${full ? 'st-full' : ''}"
                    ${full ? '' : `onclick="openSeatMap(${s.id})"`}>
            <span class="stime">${fmtTime(s.showtime)}</span>
            <span class="shall">${esc(s.hall_name)}</span>
            <span class="savail">${full ? '✕ Full' : '✓ ' + avail + ' seats'}</span>
            <span class="sprice">₦${Number(s.price).toLocaleString()}</span>
          </button>`;
        }).join('')}
      </div>
    </div>`).join('') || '<p style="color:var(--text2)">No upcoming showtimes</p>';

  ge('movie-detail').innerHTML = `
    <div class="detail-grid">
      <div>
        <img class="detail-poster" src="${esc(m.poster_url||'')}" alt="${esc(m.title)}"
             onerror="this.style.display='none'"/>
      </div>
      <div>
        <h2 class="dtitle">${esc(m.title)}</h2>
        <div class="dmeta">
          <span class="dtag rose">★ ${m.rating}</span>
          <span class="dtag">${esc(m.genre)}</span>
          <span class="dtag">${m.duration_min} min</span>
          ${m.release_year ? `<span class="dtag">${m.release_year}</span>` : ''}
          ${m.director ? `<span class="dtag">Dir. ${esc(m.director)}</span>` : ''}
        </div>
        <p class="ddesc">${esc(m.description||'')}</p>
        ${m.cast_list ? `<p style="font-size:0.78rem;color:var(--text3);margin-bottom:1.25rem">Cast: ${esc(m.cast_list)}</p>` : ''}
        <div class="sts-label">Choose a Showtime</div>
        ${stHtml}
      </div>
    </div>`;
}

// ─── SEAT MAP ──────────────────────────────────────────────────────────────
async function openSeatMap(showtimeId) {
  closeModal('movie-modal');
  openModal('seat-modal');
  ge('seat-content').innerHTML = '<div class="shimmer" style="height:400px"></div>';

  S.showtimeId   = showtimeId;
  S.selectedSeat = null;
  S.lockExpiry   = null;
  clearInterval(S.seatPoll);

  await drawSeatMap(showtimeId, false);
  S.seatPoll = setInterval(() => drawSeatMap(showtimeId, true), 8000);
}

async function drawSeatMap(showtimeId, soft) {
  const data = await api(`/api/seats/${showtimeId}`);
  if (data.error) return;
  const { seats, showtime } = data;

  // Group by row
  const rows = {};
  seats.forEach(s => { (rows[s.row_num] = rows[s.row_num]||[]).push(s); });
  const maxCol = Math.max(...seats.map(s => s.col_num));
  const mid    = Math.floor(maxCol / 2);

  const rowsHtml = Object.values(rows).map(row => {
    const cells = row.map((s, idx) => {
      const isSel = S.selectedSeat && S.selectedSeat.id === s.id;
      let cls = 'seat ';
      if      (isSel)           cls += 'selected';
      else if (s.my_lock)       cls += 'my-lock';
      else if (s.status==='booked')  cls += 'booked';
      else if (s.status==='locked')  cls += 'locked';
      else {
        const q = parseFloat(s.quality_score);
        cls += q >= 7.5 ? 'av-high' : q >= 5.0 ? 'av-mid' : 'av-low';
      }
      const clickable = s.status === 'available' || s.my_lock;
      const tags = s.position_tags ? JSON.parse(s.position_tags) : [];
      const gap  = idx === mid ? '<div class="seat-gap"></div>' : '';
      return `${gap}<div class="${cls}"
        ${clickable ? `onclick="selectSeat(${JSON.stringify(s).replace(/"/g,'&quot;')})"` : ''}
        onmouseenter="showTip(event,'${s.row_label}${s.seat_number}',${s.quality_score},'${tags.join(', ')}','${s.status}')"
        onmouseleave="hideTip()"></div>`;
    }).join('');
    return `<div class="seat-row"><div class="row-lbl">${row[0].row_label}</div>${cells}<div class="row-lbl">${row[0].row_label}</div></div>`;
  }).join('');

  const totalSeats = seats.length;
  const availSeats = seats.filter(s => s.status === 'available').length;

  if (!soft) {
    // Full render
    ge('seat-content').innerHTML = `
      <div class="seat-map-wrap">
        <div class="seat-map-head">
          <h2>${esc(showtime.title)}</h2>
          <p>${esc(showtime.cinema_name||'')} · ${esc(showtime.hall_name)} · ${fmtDateTime(showtime.showtime)} · ₦${Number(showtime.price).toLocaleString()} per seat</p>
        </div>
        <div class="screen-wrap">
          <div class="screen-bar"></div>
          <div class="screen-txt">Screen</div>
        </div>
        <div class="seat-grid-wrap"><div id="seat-inner">${rowsHtml}</div></div>
        <div class="seat-legend">
          <div class="leg-item"><div class="leg-dot ld-high"></div>Premium ≥7.5</div>
          <div class="leg-item"><div class="leg-dot ld-mid"></div>Standard 5–7.5</div>
          <div class="leg-item"><div class="leg-dot ld-low"></div>Budget &lt;5</div>
          <div class="leg-item"><div class="leg-dot ld-sel"></div>Selected</div>
          <div class="leg-item"><div class="leg-dot ld-lkd"></div>Reserved (5 min)</div>
          <div class="leg-item"><div class="leg-dot ld-bkd"></div>Booked</div>
        </div>
        <div class="avail-info" id="avail-info">${availSeats} of ${totalSeats} seats available</div>
        <div class="seat-summary hidden" id="seat-summary">
          <div class="ss-row"><span class="ss-lbl">Seat</span><span class="ss-val" id="ss-seat">—</span></div>
          <div class="ss-row"><span class="ss-lbl">Position</span><span class="ss-val" id="ss-pos">—</span></div>
          <div class="ss-row"><span class="ss-lbl">Objective Quality</span><span class="ss-val" id="ss-q">—</span></div>
          <div class="quality-bar"><div class="q-marker" id="q-marker" style="left:50%"></div></div>
          <div class="ss-row" style="margin-top:0.6rem"><span class="ss-lbl">Price</span><span class="ss-val" style="color:var(--rose)" id="ss-price">—</span></div>
          <div class="lock-timer" id="lock-timer"></div>
          <div class="book-wrap">
            <button class="btn-rose full" onclick="goToPayment()">Proceed to Payment →</button>
          </div>
        </div>
      </div>`;
  } else {
    // Soft update — only refresh grid and availability
    const inner = ge('seat-inner');
    if (inner) inner.innerHTML = rowsHtml;
    const ai = ge('avail-info');
    if (ai) ai.textContent = `${availSeats} of ${totalSeats} seats available`;
  }

  if (S.selectedSeat) updateSummary(S.selectedSeat, showtime.price);
}

// Tooltip
const TIP = ge('seat-tooltip');
document.addEventListener('mousemove', e => {
  if (TIP.classList.contains('show')) {
    TIP.style.left = (e.clientX + 14) + 'px';
    TIP.style.top  = (e.clientY - 36) + 'px';
  }
});
function showTip(e, label, quality, tags, status) {
  const statusMap = { available:'✓ Available', booked:'✕ Booked', locked:'⏳ Held' };
  TIP.innerHTML = `<strong>${label}</strong> · ${statusMap[status]||status}<br>Quality: ${quality}/10 · ${tags}`;
  TIP.classList.add('show');
  TIP.style.left = (e.clientX + 14) + 'px';
  TIP.style.top  = (e.clientY - 36) + 'px';
}
function hideTip() { TIP.classList.remove('show'); }

async function selectSeat(seatObj) {
  if (!S.user) { closeSeatModal(); openModal('login-modal'); return; }

  // Release previous different lock
  if (S.selectedSeat && S.selectedSeat.id !== seatObj.id) {
    await api('/api/seats/unlock', { method: 'POST', body: { seat_id: S.selectedSeat.id } });
  }

  const r = await api('/api/seats/lock', {
    method: 'POST',
    body: { seat_id: seatObj.id, showtime_id: S.showtimeId }
  });
  if (r.error) { toast(r.error, 'error'); return; }

  S.selectedSeat = seatObj;
  S.lockExpiry = new Date(r.locked_until + 'Z');

  await drawSeatMap(S.showtimeId, true);
  startLockTimer();

  // Fetch showtime price for summary
  const data = await api(`/api/seats/${S.showtimeId}`);
  if (data.showtime) updateSummary(seatObj, data.showtime.price);
}

function updateSummary(seat, price) {
  const tags = seat.position_tags ? JSON.parse(seat.position_tags) : [];
  const q    = parseFloat(seat.quality_score);

  const summaryEl = ge('seat-summary');
  if (!summaryEl) return;
  summaryEl.classList.remove('hidden');

  ge('ss-seat').textContent  = `${seat.row_label}${seat.seat_number}`;
  ge('ss-pos').textContent   = tags.join(', ');
  ge('ss-q').textContent     = `${q}/10 ${q>=7.5?'⭐ Premium':q>=5?'Standard':'Budget'}`;
  ge('ss-price').textContent = `₦${Number(price).toLocaleString()}`;
  ge('q-marker').style.left  = `${(q/10)*100}%`;
}

function startLockTimer() {
  clearInterval(S.lockTick);
  const el = ge('lock-timer');
  if (!el) return;
  S.lockTick = setInterval(() => {
    if (!S.lockExpiry) { clearInterval(S.lockTick); return; }
    const rem = Math.max(0, Math.ceil((S.lockExpiry - Date.now()) / 1000));
    if (el) el.textContent = rem > 0
      ? `⏱ Seat held for ${Math.floor(rem/60)}:${String(rem%60).padStart(2,'0')}`
      : '⚠ Hold expired — please reselect';
    if (rem === 0) {
      clearInterval(S.lockTick);
      S.selectedSeat = null;
      drawSeatMap(S.showtimeId, false);
      toast('Seat hold expired', 'info');
    }
  }, 1000);
}

// ─── PAYMENT ───────────────────────────────────────────────────────────────
async function goToPayment() {
  // 1. Safety Check: If no seat is selected, stop immediately
  if (!S.selectedSeat || !S.showtimeId) {
    toast("No seat selected. Please pick a seat first.", "error");
    return;
  }

  const r = await api('/api/bookings/initiate', {
    method: 'POST',
    body: { showtime_id: S.showtimeId, seat_id: S.selectedSeat.id }
  });

  if (r.error) {
    toast(r.error, 'error');
    return;
  }

  S.booking = r;
  clearInterval(S.seatPoll);

  // Capture the seat reference before closing the seat modal
  const seat = S.selectedSeat;

  closeSeatModal(); // NOTE: Your closeSeatModal() function sets S.selectedSeat to null!

  // 2. Data Recovery: Ensure position_tags is parsed correctly
  let tags = [];
  try {
    tags = typeof seat.position_tags === 'string'
           ? JSON.parse(seat.position_tags)
           : (seat.position_tags || []);
  } catch (e) {
    console.error("Error parsing tags", e);
  }

  // 3. Render Payment Content (Safe from null errors)
  ge('payment-content').innerHTML = `
    <h2 class="pay-title">Confirm Your Booking</h2>
    <p class="pay-sub">Review your selection before paying via Paystack</p>
    <div class="pay-row">
        <span class="pay-lbl">Seat</span>
        <span>${seat.row_label}${seat.seat_number} · ${tags.join(', ')}</span>
    </div>
    <div class="pay-row"><span class="pay-lbl">Seat Quality</span><span>${seat.quality_score}/10</span></div>
    <div class="pay-row"><span class="pay-lbl">Reference</span><span style="font-size:0.78rem">${r.payment_ref}</span></div>
    <div class="pay-row"><span class="pay-lbl">Amount</span><span class="pay-total">₦${Number(r.amount).toLocaleString()}</span></div>
    <div class="pay-actions">
      <button class="btn-rose full" onclick="launchPaystack()">Pay ₦${Number(r.amount).toLocaleString()} with Paystack</button>
      <button class="btn-outline full" onclick="closeModal('payment-modal')">Cancel</button>
    </div>
    <div class="paystack-note">🔒 Secured by Paystack · Cards, Bank Transfer & USSD accepted</div>`;

  openModal('payment-modal');
}
function launchPaystack() {
  const b = S.booking;
  if (!b) return;

  // Real Paystack inline popup
  const handler = PaystackPop.setup({
    key:      window.PAYSTACK_PUBLIC_KEY,
    email:    b.email,
    amount:   b.amount_kobo,          // Paystack expects kobo (₦ × 100)
    ref:      b.payment_ref,
    currency: 'NGN',
    metadata: {
      booking_id:   b.booking_id,
      custom_fields: [
        { display_name: 'Booking ID',  variable_name: 'booking_id',  value: b.booking_id },
        { display_name: 'Seat',        variable_name: 'seat',        value: `${S.selectedSeat?.row_label}${S.selectedSeat?.seat_number}` },
      ]
    },
    onClose: () => {
      toast('Payment window closed. Your seat hold is still active.', 'info');
      openModal('payment-modal');
    },
    callback: (response) => {
      // Paystack confirmed payment — verify on backend
      closeModal('payment-modal');
      confirmPayment(b.booking_id, response.reference);
    }
  });

  closeModal('payment-modal');
  handler.openIframe();
}

async function confirmPayment(bookingId, paystackRef) {
  toast('Verifying payment…', 'info');
  const r = await api('/api/bookings/verify', {
    method: 'POST',
    body: { booking_id: bookingId, paystack_ref: paystackRef }
  });
  if (r.error) { toast('Verification failed: ' + r.error, 'error'); return; }
  showConfirmation(r.booking);
}

function showConfirmation(b) {
  ge('confirm-content').innerHTML = `
    <div class="confirm-wrap">
      <div class="confirm-icon">🎟</div>
      <h2 class="confirm-title">You're all set!</h2>
      <p class="confirm-sub">Your seat is confirmed. Enjoy the film ✦</p>
      <div class="confirm-details">
        <div class="cd-row"><span class="cd-lbl">Film</span><span class="cd-val">${esc(b.title)}</span></div>
        <div class="cd-row"><span class="cd-lbl">Cinema</span><span class="cd-val">${esc(b.cinema_name||b.hall_name)}</span></div>
        <div class="cd-row"><span class="cd-lbl">Showtime</span><span class="cd-val">${fmtDateTime(b.showtime)}</span></div>
        <div class="cd-row"><span class="cd-lbl">Seat</span><span class="cd-val">${b.row_label}${b.seat_number}</span></div>
        <div class="cd-row"><span class="cd-lbl">Seat Quality</span><span class="cd-val">${b.quality_score}/10</span></div>
        <div class="cd-row"><span class="cd-lbl">Paid</span><span class="cd-val" style="color:var(--rose)">₦${Number(b.price||b.amount).toLocaleString()}</span></div>
        <div class="cd-row"><span class="cd-lbl">Ref</span><span class="cd-val" style="font-size:0.75rem">${b.paystack_ref||b.payment_ref}</span></div>
      </div>
      <button class="btn-rose full" style="margin-bottom:10px" onclick="closeModal('confirm-modal');showSection('my-bookings')">View My Tickets</button>
      <button class="btn-ghost full" onclick="closeModal('confirm-modal');showSection('browse')">Back to Movies</button>
    </div>`;
  openModal('confirm-modal');
  toast('Booking confirmed! Enjoy the show 🎬', 'success');
}

// MY TICKETS
async function loadTickets() {
  const list = ge('tickets-list');
  list.innerHTML = '<div class="shimmer"></div>';
  const bookings = await api('/api/my-bookings');

  if (!bookings.length) {
    list.innerHTML = `<div class="empty-state"><div class="ei">🎟</div><h3>No booking history yet</h3><p>Book a film to see your history here</p></div>`;
    return;
  }

  // Separate confirmed from pending
  const confirmed = bookings.filter(b => b.status === 'confirmed');
  const pending   = bookings.filter(b => b.status === 'pending');

  function ticketCard(b) {
    const tags = b.position_tags ? JSON.parse(b.position_tags) : [];
    const isConfirmed = b.status === 'confirmed';
    const badgeStyle  = isConfirmed
      ? 'background:var(--sage-dim);color:var(--sage);border:1px solid var(--sage)'
      : 'background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold)';
    const badgeText = isConfirmed ? '✓ Confirmed' : '⏳ Pending';
    const showtime  = new Date(b.showtime);
    const isPast    = showtime < new Date();

    return `<div class="ticket" style="${isPast && isConfirmed ? 'opacity:0.6' : ''}">
      <img class="tix-poster" src="${esc(b.poster_url||'')}" alt="${esc(b.title)}"
           onerror="this.style.display='none'"/>
      <div style="flex:1">
        <div class="tix-title">${esc(b.title)}</div>
        <div style="font-size:0.72rem;color:var(--rose);margin-bottom:0.3rem">${esc(b.genre||'')}</div>
        <div class="tix-meta">
          🏛 ${esc(b.cinema_name||b.hall_name)}<br>
          📅 ${fmtDateTime(b.showtime)} ${isPast ? '<span style="color:var(--text3)">(Past)</span>' : ''}<br>
          💺 Seat ${b.row_label}${b.seat_number} · ${tags.join(', ')} · Quality ${b.quality_score}/10<br>
          💳 ₦${Number(b.price||b.amount).toLocaleString()}<br>
          🔖 Ref: <span style="font-size:0.7rem;color:var(--text3)">${b.payment_ref||'—'}</span>
        </div>
      </div>
      <div style="${badgeStyle};border-radius:var(--radius-pill);padding:0.28rem 0.85rem;font-size:0.72rem;font-weight:700;white-space:nowrap;align-self:flex-start">
        ${badgeText}
      </div>
    </div>`;
  }

  let html = '';

  if (confirmed.length) {
    html += `<div style="font-family:var(--font-display);font-size:1.1rem;font-weight:700;margin-bottom:0.75rem">
               ✓ Confirmed Bookings <span style="font-size:0.8rem;color:var(--text2);font-family:var(--font-body)">(${confirmed.length})</span>
             </div>`;
    html += `<div class="tickets-list" style="margin-bottom:2rem">${confirmed.map(ticketCard).join('')}</div>`;
  }

  if (pending.length) {
    html += `<div style="font-family:var(--font-display);font-size:1.1rem;font-weight:700;margin-bottom:0.75rem">
               ⏳ Pending Payments <span style="font-size:0.8rem;color:var(--text2);font-family:var(--font-body)">(${pending.length})</span>
             </div>`;
    html += `<div class="tickets-list">${pending.map(ticketCard).join('')}</div>`;
  }

  if (!confirmed.length && !pending.length) {
    html = `<div class="empty-state"><div class="ei">🎟</div><h3>No booking history yet</h3><p>Book a film to see your history here</p></div>`;
  }

  list.innerHTML = html;
}

//  CINEMAS
async function loadCinemas() {
  const cinemas = await api('/api/cinemas');
  const icons = ['🎬','🎭','🎞','🍿'];
  ge('cinemas-grid').innerHTML = cinemas.map((c, i) => `
    <div class="cinema-card" style="animation-delay:${i*0.07}s">
      <div style="font-size:1.6rem;margin-bottom:0.5rem">${icons[i]||'🎬'}</div>
      <div class="cinema-name">${esc(c.name)}</div>
      <div class="cinema-loc">📍 ${esc(c.location)}</div>
      <div class="cinema-addr">${esc(c.address)}</div>
    </div>`).join('');
}

// ─── THEME ─────────────────────────────────────────────────────────────────
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  applyTheme(current === 'dark' ? 'light' : 'dark');
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
  const btn = ge('themeBtn');
  if (btn) btn.textContent = t === 'dark' ? '○' : '◑';
}

// ─── MODAL UTILS ───────────────────────────────────────────────────────────
function openModal(id) {
  const m = ge(id);
  if (m) m.classList.remove('hidden');
}
function closeModal(id) {
  const m = ge(id);
  if (m) m.classList.add('hidden');
}
function closeAllModals() { document.querySelectorAll('.overlay, .modal-backdrop').forEach(m => m.classList.add('hidden')) }
function closeOverlay(e, id) { if (e.target === e.currentTarget) closeModal(id) }
function switchModal(to, from) { closeModal(from); openModal(to) }
function closeSeatModal() {
  clearInterval(S.seatPoll);
  clearInterval(S.lockTick);
  if (S.selectedSeat) {
    api('/api/seats/unlock', { method:'POST', body:{ seat_id: S.selectedSeat.id } });
    S.selectedSeat = null;
  }
  closeModal('seat-modal');
}

// ─── TOAST ─────────────────────────────────────────────────────────────────
function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icons = { success:'✓', error:'✕', info:'✦' };
  el.innerHTML = `<span>${icons[type]||'✦'}</span><span>${msg}</span>`;
  ge('toast-container').appendChild(el);
  setTimeout(() => { el.style.animation = 'toastOut 0.3s ease forwards'; setTimeout(() => el.remove(), 300) }, 3800);
}

// ─── FORMAT UTILS ──────────────────────────────────────────────────────────
function fmtTime(dt)       { return new Date(dt).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) }
function fmtDateTime(dt)   {
  const d = new Date(dt);
  return d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}) + ' · ' + fmtTime(dt);
}
function fmtDateShort(dt)  { return new Date(dt).toLocaleDateString([],{month:'short',day:'numeric'}) }
function esc(s)            { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }

// ─── DOM SHORTCUTS ─────────────────────────────────────────────────────────
function ge(id)  { return document.getElementById(id) }
function val(id) { return (ge(id)||{}).value?.trim() || '' }
function hide(id){ const el = typeof id==='string'?ge(id):id; if(el) el.style.display='none' }
function show(id){ const el = typeof id==='string'?ge(id):id; if(el) el.style.display='' }
function showErr(el, msg) { el.textContent=msg; el.classList.remove('hidden') }

// ─── API ───────────────────────────────────────────────────────────────────
async function api(url, opts={}) {
  const { method='GET', body } = opts;
  try {
    const r = await fetch(url, {
      method,
      headers: {'Content-Type':'application/json'},
      body: body ? JSON.stringify(body) : undefined
    });
    return await r.json();
  } catch(e) { return {error:'Network error'} }
}
