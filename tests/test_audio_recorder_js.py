"""Testa gravacao e recuperacao de permissao usando o JS real do chat."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


TEMPLATE = Path(__file__).parents[1] / "app/templates/conversations/inbox.html"
START = "  // Gravar pela API padrão; se o Chrome negou acesso, o elemento nativo"
END = "\n  if (form && input) {"

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
const makeElement = () => {
  const events = {};
  return {
    hidden:false, disabled:false, textContent:'', open:false, track:null, error:null,
    classList:{toggle(){},add(){},remove(){}},
    addEventListener(name, fn){(events[name] ||= []).push(fn);},
    emit(name, data){for(const fn of events[name] || []) fn(data || {});},
    setConstraints(value){this.constraints=value;},
    showModal(){this.open=true;},
    close(){this.open=false;},
    cloneNode(){return makeElement();},
    replaceWith(){}
  };
};
let nativeMicrophone=makeElement();
const microphoneDialog=makeElement();
const microphoneDialogTitle=makeElement();
const microphoneDialogMessage=makeElement();
const microphoneDialogStop=makeElement();
microphoneDialogStop.hidden=true;
const microphoneDialogClose=makeElement();
const standardRecordButton=makeElement();
const recordStopButton=makeElement();
recordStopButton.hidden=true;
const recordingStatus=makeElement();
const previews=[];
const showMediaPreview=file=>previews.push(file);
const calls=[];
let denyPermission=__DENY__;
const makeTrack=()=>({kind:'audio',readyState:'live',stop(){this.readyState='ended'}});
class FakeStream {
  constructor(tracks){this.tracks=tracks||[makeTrack()]}
  getTracks(){return this.tracks}
  getAudioTracks(){return this.tracks.filter(t=>t.kind==='audio')}
  getVideoTracks(){return this.tracks.filter(t=>t.kind==='video')}
}
class MockRecorder {
  constructor(stream,options={}){this.stream=stream;this.mimeType=options.mimeType||'audio/webm';this.state='inactive'}
  static isTypeSupported(type){return type==='audio/webm;codecs=opus'||type==='audio/webm'}
  start(){
    if(this.mimeType==='audio/webm;codecs=opus') {
      const err=new Error('codec error');err.name='NotSupportedError';throw err;
    }
    if(this.state!=='inactive'){const err=new Error();err.name='InvalidStateError';throw err}
    this.state='recording';
  }
  stop(){
    this.state='inactive';
    this.ondataavailable?.({data:new Blob([new Uint8Array([1,2,3])],{type:this.mimeType})});
    this.onstop?.();
  }
}
const MediaRecorder=MockRecorder;
const MediaStream=FakeStream;
const window={
  MediaRecorder:MockRecorder,
  setInterval(){return 1},
  clearInterval(){},
  addEventListener(){}
};
if(__NATIVE__)window.HTMLMicrophoneElement=function HTMLMicrophoneElement(){};
const navigator={
  mediaDevices:{
    async getUserMedia(options){
      calls.push(options);
      if(denyPermission){
        const err=new Error('denied');err.name='NotAllowedError';throw err;
      }
      return new FakeStream();
    }
  }
};
let recorder=null;
let recorderStream=null;
let recorderChunks=[];
let recorderStartedAt=0;
let recorderTimer=null;
__LOGIC__
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  assert.equal(supportsNativeMicrophone,__NATIVE__);
  assert.equal(standardRecordButton.hidden,false);
  assert.equal(microphoneDialog.open,false,'modal nao aberto por padrao');
  standardRecordButton.emit('click');
  await tick();
  if(__DENY__ && __NATIVE__){
    assert.equal(recorder,null,'negativa nao inicia gravacao');
    assert.equal(microphoneDialog.open,true,'abre recuperacao numa modal');
    assert.equal(calls.length,1,'tentativa inicial somente');
    assert.equal(microphoneDialogMessage.textContent.includes('Chrome'),true);
    // Evento nativo InvalidStateError deve manter modal aberta e nao reabrir
    // getUserMedia enquanto o Chromium valida a posicao do controle.
    nativeMicrophone.error=new DOMException(
      'The permission element is disabled due to: intersection occluded or distorted',
      'InvalidStateError'
    );
    nativeMicrophone.emit('error');
    assert.equal(microphoneDialog.open,true);
    assert.equal(microphoneDialogMessage.textContent.includes('Aguarde'),true);
    nativeMicrophone.track=makeTrack();
    nativeMicrophone.emit('track');
    assert.equal(calls.length,1,'a faixa nativa nao pode causar segunda captura');
    assert.equal(recorder.state,'recording');
    assert.equal(microphoneDialogStop.hidden,false,'botao Parar dentro da modal');
    assert.equal(recordStopButton.hidden,true,'botao Parar oculto atras da modal');
    microphoneDialogStop.emit('click');
    assert.equal(previews.length,1);
    assert.equal(microphoneDialog.open,false,'fecha modal apos gravacao');
    assert.equal(nativeMicrophone.track,null,'novo controle nativo e criado');
    denyPermission=false;
  } else if(__DENY__ && !__NATIVE__){
    assert.equal(recorder,null);
    assert.equal(microphoneDialog.open,false);
    denyPermission=false;
    standardRecordButton.emit('click');
    await tick();
    assert.equal(recorder.state,'recording');
    recordStopButton.emit('click');
    assert.equal(previews.length,1);
  } else {
    assert.equal(recorder.state,'recording');
    assert.equal(microphoneDialog.open,false);
    assert.equal(recordStopButton.hidden,false);
    recordStopButton.emit('click');
    assert.equal(previews.length,1);
  }

  assert.equal(standardRecordButton.hidden,false,'Audio reaparece');
  assert.equal(recordStopButton.hidden,true);
  standardRecordButton.emit('click');
  await tick();
  assert.equal(recorder.state,'recording','segunda gravacao funciona');
  recordStopButton.emit('click');
  assert.equal(previews.length,2);
  assert.ok(previews.every(x=>x.size>0));
  assert.ok(calls.every(x=>x.audio===true && x.video===false),'nunca pede camera');
  const expected=__DENY__&&!__NATIVE__?3:2;
  assert.equal(calls.length,expected);
  console.log('passed');
})().catch(err=>{console.error(err.stack||err);process.exitCode=1});
"""


@pytest.mark.parametrize(
    ("native", "denied"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_recording_and_native_recovery(native, denied):
    if not shutil.which("node"):
        pytest.skip("Node.js indisponivel no CI")
    html = TEMPLATE.read_text(encoding="utf-8")
    assert START in html and END in html
    logic = html.split(START, 1)[1].split(END, 1)[0]
    js = (
        HARNESS.replace("__LOGIC__", logic)
        .replace("__NATIVE__", "true" if native else "false")
        .replace("__DENY__", "true" if denied else "false")
    )
    p = subprocess.run(["node"], input=js, text=True, capture_output=True, timeout=25)
    assert p.returncode == 0, p.stdout + "\n" + p.stderr


def test_native_button_is_outside_clipped_composer_and_never_requests_camera():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "data-native-microphone" in html
    assert "<usermedia" not in html
    assert '<microphone data-native-microphone' in html
    assert "<dialog class=\"inbox-microphone-dialog\"" in html
    first_form = html.index('class="chat-send-form"')
    close_form = html.index("</form>", first_form)
    native_button = html.index("<microphone data-native-microphone")
    assert native_button > close_form, "Chrome capability nao deve ficar dentro de overflow:hidden"
    assert "getUserMedia(audioOnlyConstraints)" in html
    assert "startRecorder(new MediaStream([track]), true)" in html
    assert "data-mic-dialog-stop" in html
    assert "video: false" in html
    assert "chrome://settings" not in html
