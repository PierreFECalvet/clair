'use strict';
// Les fichiers sont lus localement ; seul leur texte est envoyé au serveur local.
const bulkButton=document.createElement('button');
bulkButton.type='button';bulkButton.className='button';bulkButton.textContent='⇧ Importer un lot de textes';
const bulkActions=document.createElement('div');bulkActions.className='bulk-heading-actions';
$('#new-content').before(bulkActions);bulkActions.append(bulkButton,$('#new-content'));
const bulkDialog=document.createElement('dialog');bulkDialog.id='bulk-dialog';document.body.append(bulkDialog);
bulkDialog.innerHTML=`<div class="dialog-heading"><div><div class="eyebrow">IMPORT GROUPÉ</div><h2>Plusieurs textes, un seul import.</h2></div><button type="button" class="icon-button close-dialog" aria-label="Fermer">×</button></div>
<p>Jusqu’à 100 contenus par lot. Chaque texte devient un contenu distinct, avec son titre et ses chunks.</p>
<label>Ajouter plusieurs fichiers texte (.txt, UTF-8)<input id="bulk-files" type="file" accept=".txt,text/plain" multiple></label>
<p class="muted">Un fichier = un contenu. 300 000 caractères par contenu, 5 millions par lot.</p>
<label>Ou coller plusieurs contenus<textarea id="bulk-paste" rows="6" placeholder="Texte du premier contenu…&#10;&#10;===CONTENU===&#10;&#10;Texte du deuxième contenu…"></textarea></label>
<p class="muted">Séparez les contenus par une ligne contenant exactement <code>===CONTENU===</code>. Les lignes vides ne divisent pas le texte.</p>
<button type="button" class="button" id="bulk-add-paste">Ajouter ces textes à la liste</button>
<div id="bulk-error" class="notice danger" role="alert" hidden></div>
<div class="section-heading"><h3 id="bulk-count">Aucun contenu sélectionné</h3><button type="button" class="text-button" id="bulk-clear">Vider la liste</button></div>
<div id="bulk-preview"></div>
<div class="notice">L’import ne consomme aucune unité Ibou et ne lance pas l’analyse LLM. Après l’import, utilisez « Extraire & dédoublonner ».</div>
<div class="form-actions"><span class="muted">Modifiez les titres avant de lancer.</span><button type="button" class="button primary" id="bulk-submit" disabled>Importer les contenus →</button></div>`;
let bulkItems=[],bulkBusy=false;
const bulkId=()=>crypto.randomUUID().replaceAll('-','');
function splitBulkText(text){return text.replace(/\r\n?/g,'\n').split(/^===CONTENU===$/m).map(x=>x.trim()).filter(Boolean)}
function bulkError(message){$('#bulk-error').textContent=message;$('#bulk-error').hidden=!message}
function renderBulk(){
  const chars=bulkItems.reduce((n,x)=>n+Array.from(x.text).length,0);
  $('#bulk-count').textContent=`${bulkItems.length} contenu(s) · ${number(chars)} caractères`;
  $('#bulk-preview').innerHTML=bulkItems.map((x,i)=>`<div class="bulk-item"><label>Titre du contenu ${i+1}<input data-bulk-title="${x.id}" value="${esc(x.title)}" maxlength="300" required></label><p>${esc(x.text.slice(0,160))}${x.text.length>160?'…':''}</p><div><small class="muted">${number(Array.from(x.text).length)} caractères</small><button type="button" class="text-button danger" data-bulk-remove="${x.id}">Retirer</button></div></div>`).join('');
  $('#bulk-submit').disabled=bulkBusy||!bulkItems.length;
}
function appendBulk(items){
  if(bulkItems.length+items.length>100)throw Error('Maximum 100 contenus par lot. Importez ce lot avant d’en ajouter un autre.');
  if(items.some(x=>!x.text.trim()))throw Error('Un des fichiers est vide. Retirez-le de la sélection.');
  const tooLong=items.find(x=>Array.from(x.text).length>300000);
  if(tooLong)throw Error(`« ${tooLong.title} » dépasse 300 000 caractères.`);
  if([...bulkItems,...items].reduce((n,x)=>n+Array.from(x.text).length,0)>5000000)throw Error('Le lot dépasse 5 millions de caractères.');
  bulkItems.push(...items);bulkError('');renderBulk();
}
function setBulkBusy(busy){bulkBusy=busy;$('#bulk-files').disabled=busy;$('#bulk-add-paste').disabled=busy;$('#bulk-clear').disabled=busy;$('#bulk-submit').disabled=busy||!bulkItems.length;$$('[data-bulk-title],[data-bulk-remove]').forEach(el=>el.disabled=busy)}
bulkButton.onclick=()=>{bulkError('');renderBulk();bulkDialog.showModal()};
$('#bulk-add-paste').onclick=()=>{try{const blocks=splitBulkText($('#bulk-paste').value);if(!blocks.length)throw Error('Collez au moins un texte.');appendBulk(blocks.map(text=>({id:bulkId(),title:text.split('\n')[0].slice(0,100),text})));$('#bulk-paste').value=''}catch(e){bulkError(e.message)}};
$('#bulk-files').onchange=async()=>{
  const files=[...$('#bulk-files').files];setBulkBusy(true);bulkError('');
  try{
    if(files.length+bulkItems.length>100)throw Error('Maximum 100 contenus par lot.');
    if(files.reduce((n,f)=>n+f.size,0)>20000000)throw Error('La sélection dépasse 20 Mo. Importez plusieurs lots.');
    const items=[];
    for(const file of files){
      if(!/\.txt$/i.test(file.name))throw Error(`« ${file.name} » : fichier .txt requis.`);
      if(file.size>1200000)throw Error(`« ${file.name} » est trop volumineux.`);
      let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer()).trim()}catch{throw Error(`« ${file.name} » doit être enregistré en UTF-8.`)}
      items.push({id:bulkId(),title:file.name.replace(/\.txt$/i,'').slice(0,300)||'Sans titre',text});
    }
    appendBulk(items);
  }catch(e){bulkError(e.message)}finally{$('#bulk-files').value='';setBulkBusy(false)}
};
$('#bulk-preview').addEventListener('input',e=>{const id=e.target.dataset.bulkTitle;if(id){const item=bulkItems.find(x=>x.id===id);if(item)item.title=e.target.value}});
$('#bulk-preview').addEventListener('click',e=>{const b=e.target.closest('[data-bulk-remove]');if(b&&!bulkBusy){bulkItems=bulkItems.filter(x=>x.id!==b.dataset.bulkRemove);renderBulk()}});
$('#bulk-clear').onclick=()=>{bulkItems=[];bulkError('');renderBulk()};
$('#bulk-submit').onclick=async()=>{
  setBulkBusy(true);bulkError('');
  try{
    if(bulkItems.some(x=>!x.title.trim()))throw Error('Donnez un titre à chaque contenu.');
    if(new TextEncoder().encode(JSON.stringify({items:bulkItems})).length>=24000000)throw Error('Le lot est trop volumineux. Divisez-le en plusieurs imports.');
    const result=await api('import-batch',{items:bulkItems});bulkDialog.close();go('contents');await refresh();
    notify(`${result.count} contenus en cours d’import. La liste reste disponible pour reprendre le même lot sans doublons.`);
  }catch(e){bulkError(e.message)}finally{setBulkBusy(false)}
};
