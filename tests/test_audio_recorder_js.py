"""Testa cliques e gravacao do chat com API estavel e recuperacao do Chrome.

Roda a logica real de JavaScript da pagina no Node. Verifica o primeiro envio,
outra gravacao, codecs com falha, permissao negada e recuperacao in-page.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


TEMPLATE = Path(__file__).parents[1] / "app/templates/conversations/inbox.html"
START = "  // Prioridade: API de gravacao estavel, somente audio."
END = "\n  if (form && input) {"

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
const makeElement = () => {
  const events = {};
  return {
    hidden: false, disabled: false, textContent: '', title: '',
    classList: {toggle() {}, add() {}, remove() {}},
    addEventListener(name, fn) {(events[name] ||= []).push(fn);},
    emit(name) {for (const fn of events[name] || []) fn();},
    setConstraints(value) {this.constraints = value;}
  };
};
const recordButton = makeElement();
const nativeMicrophone = makeElement();
const standardRecordButton = makeElement();
const recordStopButton = makeElement();
recordStopButton.hidden = true;
const recordingStatus = makeElement();
recordingStatus.hidden = true;
const previews = [];
const showMediaPreview = file => {previews.push(file);};
const calls = [];
let denyPermission = __DENY__;
const createTrack = () => ({
  kind: 'audio', readyState: 'live',
  stop() {this.readyState = 'ended';}
});
class FakeStream {
  constructor(tracks) {this.tracks = tracks || [createTrack()];}
  getTracks() {return this.tracks;}
  getAudioTracks() {return this.tracks.filter(t => t.kind === 'audio');}
  getVideoTracks() {return this.tracks.filter(t => t.kind === 'video');}
}
class MockRecorder {
  constructor(stream, options = {}) {
    this.stream = stream;
    this.mimeType = options.mimeType || 'audio/webm';
    this.state = 'inactive';
  }
  static isTypeSupported(type) {
    return type === 'audio/webm;codecs=opus' || type === 'audio/webm';
  }
  start() {
    // Mesmo se o navegador anuncia suporte, o codec Opus pode falhar.
    if (this.mimeType === 'audio/webm;codecs=opus') {
      const error = new Error('codec error'); error.name = 'NotSupportedError';
      throw error;
    }
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({data:new Blob([new Uint8Array([1,2,3])],{type:this.mimeType})});
    this.onstop?.();
  }
}
const MediaRecorder = MockRecorder;
const MediaStream = FakeStream;
const window = {
  MediaRecorder: MockRecorder,
  setInterval() {return 1;},
  clearInterval() {},
  addEventListener() {}
};
if (__NATIVE__) window.HTMLMicrophoneElement = function HTMLMicrophoneElement() {};
const navigator = {
  mediaDevices: {
    async getUserMedia(options) {
      calls.push(options);
      if (denyPermission) {
        const err = new Error('denied');
        err.name = 'NotAllowedError';
        throw err;
      }
      return new FakeStream();
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
  assert.equal(nativeMicrophone.hidden, true, 'nao mostrar UI especial inicialmente');
  assert.equal(standardRecordButton.hidden, false, 'mostrar botao Audio normal inicialmente');
  standardRecordButton.emit('click');
  await tick();

  if (__DENY__ && __NATIVE__) {
    assert.equal(recorder, null, 'acesso negado nao pode iniciar gravacao');
    assert.equal(nativeMicrophone.hidden, false, 'liberacao nativa deve aparecer apos negar');
    assert.equal(standardRecordButton.hidden, true);
    denyPermission = false;
    nativeMicrophone.track = createTrack();
    nativeMicrophone.emit('track');
    await tick();
    assert.equal(nativeMicrophone.track.readyState, 'ended', 'liberar track do Chrome');
  }
  if (__DENY__ && !__NATIVE__) {
    assert.equal(recorder, null);
    assert.equal(standardRecordButton.hidden, false);
    denyPermission = false; // autorizacao liberada pelo controle do navegador
    standardRecordButton.emit('click');
    await tick();
  }

  assert.equal(recorder.state, 'recording', 'gravador deve iniciar no clique');
  assert.equal(recordStopButton.hidden, false, 'botao Parar aparece');
  assert.equal(standardRecordButton.hidden, true, 'botao Audio nao duplica durante captura');
  assert.equal(recorder.mimeType, 'audio/webm', 'recupera falha do codec preferido');
  recordStopButton.emit('click');
  assert.equal(previews.length, 1, 'audio precisa de previa');
  assert.ok(previews[0].size > 0);
  assert.equal(recordStopButton.hidden, true);
  assert.equal(standardRecordButton.hidden, false, 'botao Audio reaparece');
  standardRecordButton.emit('click');
  await tick();
  assert.equal(recorder.state, 'recording', 'segunda gravacao deve funcionar');
  recordStopButton.emit('click');
  assert.equal(previews.length, 2, 'segunda gravacao gera nova previa');
  assert.ok(calls.every(o => o.audio === true && o.video === false), 'nunca solicita camera');
  assert.ok(calls.length >= 2);
  console.log('pass');
})().catch(error => {console.error(error.stack || error);process.exitCode = 1;});
"""


@pytest.mark.parametrize(
    ("native", "denied"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_recording_click_stop_preview_repeat_and_permission(native, denied):
    if not shutil.which("node"):
        pytest.skip("Node.js indisponivel no CI")
    html = TEMPLATE.read_text(encoding="utf-8")
    assert START in html and END in html
    logic = html.split(START, 1)[1].split(END, 1)[0]
    script = (
        HARNESS.replace("__LOGIC__", logic)
        .replace("__NATIVE__", "true" if native else "false")
        .replace("__DENY__", "true" if denied else "false")
    )
    result = subprocess.run(
        ["node"], input=script, text=True, capture_output=True, timeout=25, check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_audio_only_and_no_manual_workaround():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "<usermedia" not in html
    assert "<camera" not in html
    assert 'data-record-standard title="Gravar áudio"' in html
    assert '<microphone class="inbox-microphone-control"' in html
    assert "getUserMedia(audioOnlyConstraints)" in html
    assert "video: false" in html
    assert "data-media-audio-preview" in html
    assert "chrome://settings" not in html
