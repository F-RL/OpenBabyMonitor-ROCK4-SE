// Minimal WHEP (WebRTC-HTTP Egress Protocol) player for MediaMTX.
// Usage: const s = await whepPlay(url, videoElement); ... await s.stop();
'use strict';

async function whepPlay(url, mediaElement) {
  // Accept page-relative URLs such as '/stream/cam/whep'.
  url = new URL(url, window.location.href).href;
  const pc = new RTCPeerConnection();
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.addTransceiver('audio', { direction: 'recvonly' });

  const stream = new MediaStream();
  pc.ontrack = (event) => {
    stream.addTrack(event.track);
    mediaElement.srcObject = stream;
  };

  await pc.setLocalDescription(await pc.createOffer());
  await waitForIceGathering(pc, 2000);

  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/sdp' },
    body: pc.localDescription.sdp,
    credentials: 'same-origin',
  });
  if (!response.ok) {
    pc.close();
    throw new Error(response.status === 404 ? 'stream not running yet' : `WHEP ${response.status}`);
  }
  // The session URL is used to end the session cleanly on stop().
  const location = response.headers.get('Location');
  const sessionUrl = location ? new URL(location, url).href : null;
  try {
    await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() });
  } catch (err) {
    pc.close();
    if (sessionUrl) fetch(sessionUrl, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
    throw err;
  }

  return {
    pc,
    async stop() {
      pc.close();
      mediaElement.srcObject = null;
      if (sessionUrl) {
        fetch(sessionUrl, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
      }
    },
  };
}

// Send the offer with all local candidates in it, so no trickle-ICE PATCH is needed.
function waitForIceGathering(pc, timeoutMs) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    pc.addEventListener('icegatheringstatechange', check);
    setTimeout(done, timeoutMs);
  });
}
