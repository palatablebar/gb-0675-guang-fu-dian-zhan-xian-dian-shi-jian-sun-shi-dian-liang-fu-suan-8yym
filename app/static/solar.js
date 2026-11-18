const solar={busy:false};const status=document.querySelector('#feedback');document.querySelector('#recompute').addEventListener('click',async()=>{if(solar.busy)return;solar.busy=true;status.textContent='复算中…';try{const dataset=document.querySelector('#dataset').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const run=await response.json();if(!response.ok)throw Error(run.detail);const url=new URL(location.href);url.searchParams.set('run_id',run.manifest.run_id);url.searchParams.set('dataset',dataset);location.href=url;}catch(error){status.textContent=error.message;}finally{solar.busy=false;}});
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}

// 断点续传：文件按固定块切片，逐块 PUT；网络中断后重新选择同一文件可凭
// session_id 续传，服务端按 (session,offset) 去重，乱序只影响连续前缀。
const up={session:null,bytes:null,name:null,chunkSize:512};
const upStatus=document.querySelector('#up-status');
const upBar=document.querySelector('#up-bar');
function upRender(s){upStatus.textContent=`连续 ${s.contiguous_bytes}/${s.total_size} 字节 · 缺口 ${s.missing_ranges.length} 段 · ${s.status}`;upBar.style.width=(s.contiguous_bytes/s.total_size*100)+'%';}
async function upSha(buf){const h=await crypto.subtle.digest('SHA-256',buf);return Array.from(new Uint8Array(h)).map(b=>b.toString(16).padStart(2,'0')).join('');}
const upFile=document.querySelector('#up-file');
if(upFile){
 document.querySelector('#up-create').addEventListener('click',async()=>{
  const f=upFile.files[0];if(!f){alert('请先选择文件');return;}
  if(f.size>2000000){alert('文件超过2MB');return;}
  const buf=await f.arrayBuffer();up.bytes=new Uint8Array(buf);up.name=f.name;
  const r=await fetch('/api/uploads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({total_size:f.size,sha256:await upSha(buf),filename:f.name})});
  const d=await r.json();if(!r.ok)throw Error(d.detail);up.session=d.session_id;upRender(d);
 });
 document.querySelector('#up-preview').addEventListener('click',()=>{if(!up.session){alert('请先创建会话');return;}location.href='/uploads/preview?session_id='+up.session;});
 document.querySelector('#up-finalize').addEventListener('click',async()=>{
  if(!up.session){alert('请先创建会话');return;}
  const r=await fetch('/api/uploads/'+up.session+'/finalize',{method:'POST'});const d=await r.json();
  if(!r.ok){alert(d.detail);return;}location.href='/intake?run_id='+d.manifest.run_id;
 });
 // 依次上传所有分块；失败的块记录下来，最后统一补传，验证“断点续跑不重复采信”。
 document.querySelector('#up-file').insertAdjacentHTML('afterend','<label class="muted"><input type="checkbox" id="up-chaos"> 模拟中断与乱序（随机跳过约30%分块，随后补传）</label>');
 upFile.addEventListener('change',()=>{up.session=null;upBar.style.width='0%';upStatus.textContent='未创建会话';});
 const btn=document.createElement('button');btn.textContent='开始/续传分块';btn.id='up-send';
 document.querySelector('#up-finalize').before(btn);
 btn.addEventListener('click',async()=>{
  if(!up.session){alert('请先创建会话');return;}
  btn.disabled=true;const chaos=document.querySelector('#up-chaos').checked;
  const offsets=[];for(let o=0;o<up.bytes.length;o+=up.chunkSize)offsets.push(o);
  const order=chaos?offsets.map(o=>[Math.random(),o]).sort((a,b)=>a[0]-b[0]).map(x=>x[1]):offsets;
  const skipped=[];
  for(const offset of order){
   if(chaos&&Math.random()<0.3){skipped.push(offset);continue;}
   const part=up.bytes.slice(offset,Math.min(offset+up.chunkSize,up.bytes.length));
   const r=await fetch('/api/uploads/'+up.session+'/chunks/'+offset,{method:'PUT',body:part});
   const d=await r.json();if(!r.ok){alert(d.detail);break;}upRender(d);
  }
  for(const offset of skipped){
   const part=up.bytes.slice(offset,Math.min(offset+up.chunkSize,up.bytes.length));
   const r=await fetch('/api/uploads/'+up.session+'/chunks/'+offset,{method:'PUT',body:part});
   const d=await r.json();if(!r.ok){alert(d.detail);break;}upRender(d);
  }
  const s=await (await fetch('/api/uploads/'+up.session)).json();upRender(s);btn.disabled=false;
 });
}
