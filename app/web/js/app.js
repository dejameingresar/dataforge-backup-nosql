/* DataForge Backup — la interfaz.
 *
 * Regla de oro: esta capa no calcula nada. Cada cifra que se pinta viene de la
 * respuesta del servidor, y lo que no llega se muestra como "n/d". Nunca se
 * inventa un numero en el navegador.
 */
'use strict';

const NOMBRES = {
  mongodb: 'MongoDB',
  redis: 'Redis',
  neo4j: 'Neo4j',
};

const $ = (sel) => document.querySelector(sel);

/* ------------------------------------------------------------- utilidades */
function nd(valor) {
  // "no disponible": lo que el servidor no midio, se dice que no se midio.
  if (valor === null || valor === undefined || valor === '') return 'n/d';
  return valor;
}

function bytes(n) {
  if (n === null || n === undefined || isNaN(n)) return 'n/d';
  const num = Number(n);
  if (num < 1024) return `${num} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KiB`;
  return `${(num / (1024 * 1024)).toFixed(2)} MiB`;
}

function milisegundos(n) {
  if (n === null || n === undefined || isNaN(n)) return 'n/d';
  const num = Number(n);
  if (num < 1000) return `${num.toFixed(1)} ms`;
  return `${(num / 1000).toFixed(2)} s`;
}

function esc(texto) {
  const div = document.createElement('div');
  div.textContent = texto === null || texto === undefined ? '' : String(texto);
  return div.innerHTML;
}

function lista(items, clase) {
  if (!items || !items.length) return '<span class="vacio">n/d</span>';
  return `<ul class="${clase || ''}">` +
    items.map((t) => `<li>${esc(t)}</li>`).join('') + '</ul>';
}

function aviso(texto, clase) {
  const el = $('#estado');
  el.textContent = texto;
  el.className = `estado ${clase || ''}`;
  el.hidden = false;
}

/* ---------------------------------------------------------------- motores */
function pintaMotores(datos) {
  const cont = $('#motores');
  const motores = (datos && datos.motores) || {};
  const claves = Object.keys(motores);
  if (!claves.length) {
    cont.innerHTML = '<p class="vacio">El servidor no devolvio motores.</p>';
    return;
  }
  cont.innerHTML = claves.map((clave) => {
    const m = motores[clave] || {};
    const ok = !!m.conectado;
    const filas = [
      ['Version', nd(m.version)],
      ['Latencia', m.latencia_ms === null || m.latencia_ms === undefined
        ? 'n/d' : milisegundos(m.latencia_ms)],
    ];
    if (m.detalle) filas.push(['Detalle', m.detalle]);
    return `<article class="motor ${ok ? 'conectado' : 'caido'}" data-motor="${esc(clave)}">
      <h3>${esc(NOMBRES[clave] || clave)} <span class="punto">${ok ? 'conectado' : 'sin conexion'}</span></h3>
      <dl>${filas.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
      ${m.error ? `<p class="err">${esc(m.error)}</p>` : ''}
    </article>`;
  }).join('');

  const arriba = claves.filter((k) => motores[k] && motores[k].conectado).length;
  $('#resumen-motores').textContent = `${arriba} de ${claves.length} contestando`;
}

/* ------------------------------------------------------------ estrategias */
function pintaEstrategias(datos) {
  const cont = $('#estrategias');
  const catalogo = (datos && datos.estrategias) || [];
  if (!catalogo.length) {
    cont.innerHTML = '<p class="vacio">El nucleo no declaro estrategias todavia.</p>';
    return;
  }
  cont.innerHTML = `<table id="tabla-estrategias">
    <thead><tr>
      <th>Id</th><th>Estrategia</th><th>Que protege</th>
      <th>Que NO protege</th><th>Motores</th>
    </tr></thead>
    <tbody>${catalogo.map((e) => `<tr data-estrategia="${esc(e.id)}">
      <td class="id">${esc(e.id)}</td>
      <td class="nombre">${esc(e.nombre || e.id)}</td>
      <td>${lista(e.protege)}</td>
      <td class="no">${lista(e.no_protege)}</td>
      <td>${esc((e.soporta || []).join(', ') || 'n/d')}</td>
    </tr>`).join('')}</tbody></table>`;

  $('#resumen-estrategias').textContent = `${catalogo.length} declaradas`;
  const sel = $('#estrategia');
  sel.innerHTML = catalogo.map((e) =>
    `<option value="${esc(e.id)}">${esc(e.nombre || e.id)}</option>`).join('');
}

/* -------------------------------------------------------------- resultado */
function pintaResultado(datos, accion) {
  const cont = $('#resultado');
  const ok = !!datos.ok;
  const titulo = accion === 'restaurar' ? 'Restauracion' : 'Respaldo';
  const sello = ok
    ? '<span class="sello ok">completado</span>'
    : '<span class="sello mal">con error</span>';

  let html = `<p class="titulo"><strong>${titulo} ${esc(NOMBRES[datos.motor] || datos.motor || '')}</strong>
     · estrategia <code>${esc(datos.estrategia || '')}</code> ${sello}</p>`;

  if (datos.error) {
    html += `<p class="vacio">Error: ${esc(datos.error)}</p>`;
  }

  const filas = [
    ['Registros', nd(datos.registros)],
    ['Tamano del artefacto', bytes(datos.bytes)],
    ['Duracion', milisegundos(datos.duracion_ms)],
  ];
  html += `<table id="tabla-resultado"><tbody>${filas.map(([k, v]) =>
    `<tr><th>${esc(k)}</th><td data-campo="${esc(k)}">${esc(v)}</td></tr>`).join('')}</tbody></table>`;

  if (datos.sha256) {
    html += `<p>Hash sha256 (recalculado leyendo el archivo):` +
      `<span class="hash" id="hash">${esc(datos.sha256)}</span></p>`;
  } else {
    html += '<p class="vacio">Hash sha256: n/d (no se genero artefacto)</p>';
  }

  if (datos.verificado !== undefined && datos.verificado !== null) {
    html += `<p class="vacio">Verificacion: ${datos.verificado
      ? 'el hash coincide' : 'EL HASH NO COINCIDE'}</p>`;
  }
  if (datos.ruta) html += `<p class="vacio">Artefacto: ${esc(datos.ruta)}</p>`;
  if (datos.detalle) html += `<p class="vacio">${esc(datos.detalle)}</p>`;

  cont.innerHTML = html;
  $('#resultado-titulo').textContent = ok ? 'ultima ejecucion' : 'ultima ejecucion fallo';
}

/* ------------------------------------------------------------- peticiones */
async function pide(ruta, opciones) {
  const res = await fetch(ruta, opciones || {});
  let datos = null;
  try {
    datos = await res.json();
  } catch (e) {
    throw new Error(`respuesta no JSON (HTTP ${res.status})`);
  }
  if (!res.ok) throw new Error(datos.error || `HTTP ${res.status}`);
  return datos;
}

async function cargaMotores() {
  try {
    const datos = await pide('/api/motores');
    pintaMotores(datos);
  } catch (e) {
    $('#motores').innerHTML = `<p class="vacio">No se pudo consultar: ${esc(e.message)}</p>`;
    aviso('No se pudo consultar el estado de los motores: ' + e.message, 'error');
  }
}

async function cargaEstrategias() {
  try {
    const datos = await pide('/api/estrategias');
    pintaEstrategias(datos);
  } catch (e) {
    $('#estrategias').innerHTML = `<p class="vacio">No se pudo cargar: ${esc(e.message)}</p>`;
  }
}

function trabajando(activo, texto) {
  const ids = ['#btn-respaldar', '#btn-restaurar'];
  ids.forEach((s) => { $(s).disabled = activo; });
  $('#progreso').hidden = !activo;
  if (activo) aviso(texto, 'trabajando');
}

async function ejecuta(accion) {
  const motor = $('#motor').value;
  const estrategia = $('#estrategia').value;
  if (!estrategia) {
    aviso('Elige una estrategia antes de ejecutar.', 'error');
    return;
  }
  trabajando(true, accion === 'restaurar'
    ? `Restaurando ${motor} con ${estrategia}...`
    : `Respaldando ${motor} con ${estrategia}...`);
  try {
    const datos = await pide(`/api/${accion}/${motor}/${estrategia}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    pintaResultado(datos, accion);
    aviso(accion === 'restaurar'
      ? `Restauracion de ${motor} completada`
      : `Respaldo de ${motor} generado`, datos.ok ? 'ok' : 'error');
    await cargaMotores();
  } catch (e) {
    pintaResultado({ ok: false, motor, estrategia, error: e.message }, accion);
    aviso('Error: ' + e.message, 'error');
  } finally {
    trabajando(false, '');
  }
}

/* ------------------------------------------------------------- arranque */
function init() {
  $('#btn-respaldar').addEventListener('click', () => ejecuta('respaldar'));
  $('#btn-restaurar').addEventListener('click', () => ejecuta('restaurar'));
  $('#btn-refrescar').addEventListener('click', cargaMotores);
  $('#motor').addEventListener('change', cargaMotores);
  cargaMotores();
  cargaEstrategias();
}

document.addEventListener('DOMContentLoaded', init);