(() => {
    let running = false;
    const content = () => document.getElementById('dashboard-content');
    const status = (message, signIn = false) => {
        const root = content();
        const target = root.querySelector('[data-dashboard-status]');
        target.textContent = message;
        if (signIn) {
            const link = document.createElement('a');
            link.href = root.dataset.loginUrl;
            link.textContent = ' Sign in again';
            target.appendChild(link);
        }
    };
    const setBusy = (busy) => {
        const button = content().querySelector('[data-dashboard-refresh]');
        button.disabled = busy;
        button.textContent = busy ? 'Updating sales…' : '↻ Update sales';
    };

    async function fetchSavedSummary() {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        let response, html;
        try {
            response = await fetch(content().dataset.summaryUrl, {cache: 'no-store', signal: controller.signal});
            html = await response.text();
        } finally {
            clearTimeout(timeout);
        }
        if (response.redirected || response.status === 401 || response.status === 403) {
            throw new Error('sign-in');
        }
        if (!response.ok) throw new Error('summary');
        const template = document.createElement('template');
        template.innerHTML = html;
        const next = template.content.querySelector('#dashboard-content');
        if (!next) throw new Error('summary');
        content().replaceWith(next);
        setBusy(true);
    }

    async function updateSales(manual = false) {
        if (running || !content()) return;
        const initial = content();
        const stores = JSON.parse(manual ? initial.dataset.allStores : initial.dataset.syncStores);
        if (!stores.length) {
            status('Showing the latest saved sales.');
            return;
        }
        running = true;
        setBusy(true);
        const pending = [...stores];
        const failed = new Set();
        const busy = new Set();
        const started = Date.now();
        let authExpired = false;
        try {
            // At most three pages per store per visit/click. Large initial imports
            // continue in the scheduled job or Sales Report, without an endless spinner.
            for (let round = 0; round < 3 && pending.length; round++) {
                const batch = pending.splice(0);
                for (let index = 0; index < batch.length; index++) {
                    if (Date.now() - started > 45000) {
                        pending.push(...batch.slice(index));
                        break;
                    }
                    const store = batch[index];
                    status(`Updating ${store} sales… Your saved figures remain available.`);
                    try {
                        const root = content();
                        const controller = new AbortController();
                        const timeout = setTimeout(() => controller.abort(), 60000);
                        let response, result;
                        try {
                            response = await fetch(root.dataset.syncUrl, {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json', 'X-Sales-CSRF': root.dataset.csrf},
                                body: JSON.stringify({store}),
                                signal: controller.signal,
                            });
                            if (response.redirected || response.status === 401 || response.status === 403) {
                                throw new Error('sign-in');
                            }
                            if (!response.ok) throw new Error('sync');
                            result = await response.json();
                        } finally {
                            clearTimeout(timeout);
                        }
                        if (result.busy) busy.add(store);
                        else if (result.more) pending.push(store);
                    } catch (error) {
                        if (error.message === 'sign-in') throw error;
                        failed.add(store);
                    }
                }
                // Render each committed round from the database; never discard saved
                // figures just because a store or the next batch cannot be reached.
                await fetchSavedSummary();
                if (Date.now() - started > 45000) break;
            }
            if (failed.size) {
                status(`Could not update ${[...failed].join(', ')}. Saved figures are still shown. Use Update sales to retry.`);
            } else if (pending.length || busy.size) {
                status('Saved figures updated. More sales are still syncing; use Update sales again or continue in Sales Report.');
            } else {
                status('Sales updated and saved. Loyalty updates separately on its schedule.');
            }
        } catch (error) {
            authExpired = error.message === 'sign-in';
            status(authExpired ? 'Your session has ended. Saved figures remain on screen.' :
                'The update could not finish. Saved figures remain available; use Update sales to retry.', authExpired);
        } finally {
            running = false;
            setBusy(false);
            if (authExpired) content().querySelector('[data-dashboard-refresh]').disabled = true;
        }
    }

    document.addEventListener('click', (event) => {
        if (event.target.closest('[data-dashboard-refresh]')) updateSales(true);
    });
    // Old bookmarks with refresh=1 no longer force a live rebuild on reload.
    const url = new URL(window.location.href);
    if (url.searchParams.has('refresh')) {
        url.searchParams.delete('refresh');
        window.history.replaceState(null, '', url);
    }
    // Defer source requests until the saved page has been painted.
    requestAnimationFrame(() => setTimeout(() => updateSales(), 0));
})();
