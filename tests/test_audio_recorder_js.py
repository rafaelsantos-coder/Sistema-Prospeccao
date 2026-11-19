"""Teste executavel do gravador no clique usando mocks de DOM e MediaRecorder.

Garante que Chrome 153+ usa so audio, que o usuario consegue parar e
gravar novamente e que navegadores antigos usam o fallback sem camera.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


TEMPLATE = Path(__file__).parents[1] / "app/templates/conversations/inbox.html"
START = "  // Chrome 153+: <microphone> (somente audio, sem camera)."
END = "\n  if (form && input) {"

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
const makeElement = () => {
  const events = {};
  return {
    hidden: false, disabled: false, textContent: '', title: '',
    classList: {toggle() {}, add() {}, remove() {}},
    addEventListener(name, callback) { (events[name] ||= []).push(callback); },
    emit(name) { for (const callback of events[name] || []) callback(); },
    setConstraints(constraints) { this.constraints = constraints; },
    cloneNode() { return makeElement(); },
    replaceWith() {},
  };
};
const recordButton = makeElement();
let nativeMicrophone = makeElement();
const standardRecordButton = makeElement();
standardRecordButton.hidden = true;
const recordStopButton = makeElement();
recordStopButton.hidden = true;
const recordingStatus = makeElement();
recordingStatus.hidden = true;
const mediaCaption = {placeholder: ''};
const previews = [];
const calls = [];
const showMediaPreview = file => { previews.push(file); };
const createTrack = () => ({
  kind: 'audio',
  readyState: 'live',
  stop() { this.readyState = 'ended'; }
});
class MockStream {
  constructor(tracks) { this.tracks = tracks || [createTrack()]; }
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks.filter(track => track.kind === 'audio'); }
  getVideoTracks() { return this.tracks.filter(track => track.kind === 'video'); }
}
class MockRecorder {
  constructor(stream, options = {}) {
    this.stream = stream;
    this.mimeType = options.mimeType || 'audio/webm';
    this.state = 'inactive';
  }
  static isTypeSupported(type) { return type === 'audio/webm'; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({data: new Blob([new Uint8Array([1, 2, 3])], {type: this.mimeType})});
    this.onstop?.();
  }
}
const MediaStream = MockStream;
const MediaRecorder = MockRecorder;
const window = {
  MediaRecorder: MockRecorder,
  setInterval() { return 1; },
  clearInterval() {},
  addEventListener() {}
};
if (__NATIVE__) window.HTMLMicrophoneElement = function HTMLMicrophoneElement() {};
const navigator = {
  mediaDevices: {
    async getUserMedia(options) {
      calls.push(options);
      return new MockStream();
    }
  }
};
let recorder = null;
let recorderStream = null;
let recorderChunks = [];
let recorderStartedAt = 0;
let recorderTimer = null;

__LOGIC__

const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  assert.equal(supportsNativeMicrophone, __NATIVE__);
  if (__NATIVE__) {
    assert.deepEqual(nativeMicrophone.constraints, {echoCancellation:true, noiseSuppression:true});
    assert.equal(calls.length, 0);
    nativeMicrophone.track = createTrack();
    nativeMicrophone.emit('track');
  } else {
    recordButton.emit('click');
    await tick();
  }
  assert.equal(recorder.state, 'recording', 'primeiro clique deve iniciar gravacao');
  assert.equal(recordStopButton.hidden, false, 'botao Parar precisa aparecer');
  recordStopButton.emit('click');
  assert.equal(previews.length, 1, 'audio precisa gerar pre-visualizacao');
  assert.ok(previews[0].size > 0);
  assert.equal(recordStopButton.hidden, true, 'Parar deve desaparecer');
  assert.equal(standardRecordButton.hidden, false, 'botao Audio deve reaparecer');
  standardRecordButton.emit('click');
  await tick();
  assert.equal(recorder.state, 'recording', 'segunda gravacao precisa iniciar');
  recordStopButton.emit('click');
  assert.equal(previews.length, 2, 'segunda gravacao precisa gerar audio');
  assert.equal(recordStopButton.hidden, true);
  assert.equal(standardRecordButton.hidden, false);
  assert.ok(calls.every(options => options.video === false && options.audio.echoCancellation === true));
  if (__NATIVE__) assert.equal(calls.length, 1, 'o primeiro acesso nativo nao usa getUserMedia');
  else assert.equal(calls.length, 2, 'fallback utiliza getUserMedia em cada gravacao');
  console.log('ok');
})().catch(error => {console.error(error.stack || error); process.exitCode = 1;});
"""


@pytest.mark.parametrize("native", [True, False])
def test_recording_click_stop_preview_and_repeat(native):
    if not shutil.which("node"):
        pytest.skip("Node.js nao esta disponivel no CI")
    template = TEMPLATE.read_text(encoding="utf-8")
    assert START in template and END in template
    code = template.split(START, 1)[1].split(END, 1)[0]
    js = HARNESS.replace("__LOGIC__", code).replace("__NATIVE__", "true" if native else "false")
    result = subprocess.run(
        ["node"], input=js, text=True, capture_output=True, timeout=25, check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_never_request_camera_or_show_camera_control():
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "<usermedia" not in template
    assert "<camera" not in template
    assert '<microphone class="inbox-microphone-control"' in template
    assert "HTMLMicrophoneElement" in template
    assert "video: false" in template
    assert "data-mic-copy-settings" not in template
