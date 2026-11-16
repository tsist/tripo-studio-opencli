import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const args=process.argv.slice(2);
if(args.length!==2||args[0]!=='--destination'||!path.isAbsolute(args[1])){
 console.error('Usage: node scripts/install-adapters.mjs --destination <absolute OpenCLI clis/tripo-studio directory>');process.exit(2);
}
const source=fileURLToPath(new URL('../adapters/tripo-studio/',import.meta.url));
const destination=path.resolve(args[1]);
const names=['_shared.mjs','assets.js','balance.js','export-url.js','progress.js','project.js','quota.js'];
for(const name of names){
 if(!fs.statSync(path.join(source,name)).isFile())throw Error('Missing packaged adapter: '+name);
 if(fs.existsSync(path.join(destination,name)))throw Error('Refusing to overwrite: '+name);
}
fs.mkdirSync(destination,{recursive:true});
for(const name of names)fs.copyFileSync(path.join(source,name),path.join(destination,name),fs.constants.COPYFILE_EXCL);
console.log(JSON.stringify({installed:names.length,destination,dependencies_installed:false}));
