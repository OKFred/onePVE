// Dedicated loopback proof only; real shader execution and exact pixel readback.
window.runGpuProbe = async () => {
  const contexts = [];
  for (const api of ['webgl', 'webgl2']) {
    const canvas = document.getElementById(api);
    const gl = canvas.getContext(api, {preserveDrawingBuffer: true, antialias: false});
    if (!gl) throw new Error(api + ' context unavailable');
    const newer = api === 'webgl2';
    const vertex = newer ? '#version 300 es\nin vec2 p; void main(){gl_Position=vec4(p,0.,1.);}' : 'attribute vec2 p; void main(){gl_Position=vec4(p,0.,1.);}';
    const fragment = newer ? '#version 300 es\nprecision mediump float; out vec4 c; uniform vec4 color; void main(){c=color;}' : 'precision mediump float; uniform vec4 color; void main(){gl_FragColor=color;}';
    const compile = (kind, code) => {
      const shader = gl.createShader(kind); gl.shaderSource(shader, code); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
      return shader;
    };
    const vs = compile(gl.VERTEX_SHADER, vertex), fs = compile(gl.FRAGMENT_SHADER, fragment);
    const program = gl.createProgram(); gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
    const location = gl.getAttribLocation(program, 'p'); gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0);
    const colors = [[0,255,0,255],[220,40,140,255]];
    for (const color of colors) {
      gl.uniform4f(gl.getUniformLocation(program, 'color'), ...color.map(v => v / 255));
      gl.drawArrays(gl.TRIANGLES, 0, 3); gl.finish();
      const pixel = new Uint8Array(4); gl.readPixels(10,10,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);
      if (gl.getError() !== gl.NO_ERROR || pixel.some((v,i) => v !== color[i])) throw new Error(api + ' shader/readPixels mismatch');
    }
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (!ext) throw new Error(api + ' renderer identity unavailable');
    contexts.push({api, draw_readback_pass: true, unmasked_renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),
                   unmasked_vendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)});
    gl.deleteBuffer(buffer); gl.deleteProgram(program); gl.deleteShader(vs); gl.deleteShader(fs);
  }
  const result = {contexts}; document.getElementById('gpu-result').textContent = JSON.stringify(result, null, 2);
  return result;
};
