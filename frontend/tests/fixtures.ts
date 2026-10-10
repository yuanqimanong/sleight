export function response(data:unknown){return {ok:true,json:async()=>data}}
export function consoleData(url:string){
  const path=new URL(url).pathname
  if(path.endsWith('/fleet'))return {instances:[],counts:{all:0,running:0,persistent:0,temporary:0},warnings:[]}
  if(path.endsWith('/metrics'))return {host:{cpu_percent:12,memory_percent:24,memory_total:1000000000,memory_used:240000000},containers:[]}
  if(path.endsWith('/kernels'))return [{name:'Chrome',binary:'chrome.exe',kind:'native',extensions:'manual',version:'155'}]
  if(path.endsWith('/kernels/check'))return {name:'Chromium',binary:'D:/chromium/chrome.exe',kind:'native',extensions:'automatic'}
  if(path.endsWith('/launch-templates'))return [{id:'builtin-native',kind:'native',name:'本机原生',config:{headless:false}},{id:'builtin-cloak-0',kind:'cloak',name:'Win-US-00',config:{}}]
  if(path.endsWith('/fleet/name'))return {available:true}
  if(path.endsWith('/manager-images'))return {images:[],warning:''}
  return undefined
}
