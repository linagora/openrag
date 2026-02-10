// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import tailwindcss from '@tailwindcss/vite';
import mermaid from 'astro-mermaid';


// https://astro.build/config
export default defineConfig({
  srcDir: './docs',
  base: '/openrag/',
  integrations: [
      starlight({
          title: 'Docs',
          customCss:[
            './docs/styles/global.css',
            './docs/styles/custom.css',
            '@fontsource-variable/space-grotesk',
          ],
          logo: {
              src: './docs/assets/OpenRAG-title.svg',  
          },
          editLink:{
              baseUrl: 'https://github.com/linagora/openrag/edit/main',
          },
          tableOfContents:{
            minHeadingLevel:2,
            maxHeadingLevel:6,
          },
          social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/linagora/openrag' }],
          sidebar: [
              {
                  label: 'Home',
                  slug: 'index'
              },
              {
                  label: 'Getting Started',
                  autogenerate: { directory: 'getting_started' }
              },
              {
                  label: 'Installation',
                  autogenerate: { directory: 'installation' }
              },
              {
                  label: 'Docs',
                  autogenerate: { directory: 'documentation' }
              },
              {
                  label: 'Support and Contribute',
                  slug: 'support-and-contribute'
              },
              {
                  label: 'License',
                  slug: 'license'
              }
          ],
      }),
      mermaid({
        theme: 'default',
        autoTheme: true
      })
  ],

  vite: {
    plugins: [tailwindcss()],
  },
});