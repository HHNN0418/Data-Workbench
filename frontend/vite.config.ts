import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],server:{proxy:{'/api':'http://127.0.0.1:8765'}},build:{chunkSizeWarningLimit:1100,rollupOptions:{output:{manualChunks(id){
 // Libraries change far less often than pages; separate chunks stay cached across deploys.
 const path=id.replaceAll('\\','/');
 if(!path.includes('/node_modules/'))return;
 if(/\/node_modules\/(echarts|zrender)\//.test(path))return 'vendor-echarts';
 if(/\/node_modules\/(react|react-dom|scheduler|react-router|react-router-dom|@tanstack)\//.test(path))return 'vendor-react';
}}}}});
