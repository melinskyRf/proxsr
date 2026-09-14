const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function dashboard() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            innerHTML: '', textContent: '', value: '', scrollLeft: 0,
            contains: () => false, querySelectorAll: () => [],
        });
        return elements.get(id);
    };
    const context = vm.createContext({document: {
        addEventListener() {}, createElement: () => ({}), head: {appendChild() {}},
        getElementById: element, activeElement: null,
    }, console, setTimeout() {}});
    vm.runInContext(fs.readFileSync('web/static/app.js', 'utf8'), context);
    vm.runInContext(`currentData.proxies = [
        {id:'a',name:'Brasil <principal>',type:'rotating',host:'a.local',port:80,protocol:'http',enabled:false,is_alive:true,current_rotations:0,rotation_count:10,total_requests:0,failures:0},
        {id:'b',name:'Reserva',type:'rotating',host:'b.local',port:80,protocol:'http',enabled:true,is_alive:true,current_rotations:2,rotation_count:10,total_requests:2,failures:0}
    ]; currentData.rotator = {strategy:'cycle',cycle_order:['b'],last_proxy_id:'a'};`, context);
    return {context, element};
}

test('map retains disabled proxies and escapes their names', () => {
    const {context, element} = dashboard();
    vm.runInContext('renderRotation()', context);
    const markup = element('rotation-visual').innerHTML;
    assert.match(markup, /Brasil &lt;principal&gt;/);
    assert.match(markup, /Desativada/);
    assert.doesNotMatch(markup, /connection-node is-selected/);
    assert.equal(element('rot-next').textContent, 'Reserva');
});

test('unchanged refresh preserves the map DOM and horizontal position', () => {
    const {context, element} = dashboard();
    vm.runInContext('renderRotation()', context);
    const map = element('rotation-visual');
    map.scrollLeft = 150;
    Object.defineProperty(map, 'innerHTML', {get: () => map._markup, set: () => {throw Error('Unnecessary DOM replacement');}});
    vm.runInContext('renderRotation()', context);
    assert.equal(map.scrollLeft, 150);
});

test('search filters proxies by name and host', () => {
    const {context, element} = dashboard();
    element('proxy-search').value = 'reserva';
    vm.runInContext('renderProxies()', context);
    assert.match(element('rotating-proxies-list').innerHTML, /Reserva/);
    assert.doesNotMatch(element('rotating-proxies-list').innerHTML, /Brasil/);
    element('proxy-search').value = 'a.local';
    vm.runInContext('renderProxies()', context);
    assert.match(element('rotating-proxies-list').innerHTML, /Brasil/);
});

test('random rotation never advertises a predictable next proxy', () => {
    const {context, element} = dashboard();
    vm.runInContext("currentData.rotator.strategy='random'; renderRotation()", context);
    assert.equal(element('rot-next').textContent, 'Definida na seleção');
});
