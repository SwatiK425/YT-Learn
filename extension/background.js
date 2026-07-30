// ─── Praxis Service Worker ─────────────────────────
// Handles extension icon clicks and opens Praxis on YouTube.

chrome.action.onClicked.addListener((tab) => {
  if (tab.url && tab.url.includes('youtube.com/watch')) {
    // User is on a YouTube video — toggle the overlay
    chrome.tabs.sendMessage(tab.id, { action: 'openPraxis' }).catch(() => {
      // Content script not ready yet — push them to YouTube
      chrome.tabs.update(tab.id, { url: 'https://www.youtube.com' });
    });
  } else {
    // Not on a video — open YouTube so they can find one
    chrome.tabs.create({ url: 'https://www.youtube.com' });
  }
});
