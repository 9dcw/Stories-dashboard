import json
from pathlib import Path
from playwright.sync_api import sync_playwright
import argparse
parser=argparse.ArgumentParser(description='Check category filtering with fixture data; never modifies server records.')
parser.add_argument('--base-url',help='Check deployed static assets instead of local files')
args=parser.parse_args()
root=Path(__file__).resolve().parents[2]
model={'stories':[
 {'story_id':1,'title':'Ready Phase 2 story','status':'Phase 2','source':'Court'},
 {'story_id':2,'title':'Pending Phase 2 story','status':'Phase 2 pending','source':'Court','phase2_job':{'state':'queued','step':'drive','attempts':0}},
 {'story_id':3,'title':'Failed Phase 2 story','status':'Phase 2 pending','source':'Agency','phase2_job':{'state':'failed','step':'drive','attempts':1,'error':'Timeout'}},
 {'story_id':4,'title':'Phase 3 story','status':'Phase 3','source':'Agency'}],
 'recent_candidates':[
 {'item_id':10,'headline':'Court candidate','source':'Court','status':'NEW','lane':'B','summary_status':'PENDING'},
 {'item_id':11,'headline':'Agency candidate','source':'Agency','status':'NEW','lane':'A','summary_status':'COMPLETE'},
 {'item_id':12,'headline':'Failed summary','source':'Court','status':'NEW','summary_status':'FAILED'},
 {'item_id':13,'headline':'Review candidate','source':'Court','status':'NEW','screening':{'decision':'PASS','needs_review':True}},
 {'item_id':14,'headline':'Dismissed candidate','source':'Court','status':'DISMISSED'},
 {'item_id':15,'headline':'Excluded candidate','source':'Court','status':'NEW','screening':{'decision':'EXCLUDED'}}],
 'sources':[{'source_id':1,'name':'Court','lane':'B','active':True,'last_error':'Source timeout'}, {'source_id':2,'name':'Agency','lane':'A','active':False,'last_error':''}],
 'source_proposals':[{'name':'New publication','status':'PENDING'}]}
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True)
 for width in [1280,390]:
  page=browser.new_page(viewport={'width':width,'height':900})
  errors=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  def handle(route):
   from urllib.parse import urlparse
   path=urlparse(route.request.url).path
   if route.request.url.startswith('https://accounts.google.com'):
    route.abort();return
   if path=='/api/v1/config': route.fulfill(json={'google_client_id':'test'});return
   if path=='/api/v1/auth/session': route.fulfill(json={'csrf_token':'test'});return
   if path=='/api/v1/dashboard': route.fulfill(json=model);return
   if args.base_url:
    route.continue_();return
   name=path.lstrip('/') or 'index.html'
   file=root/name
   if file.is_file():route.fulfill(path=str(file));return
   route.fulfill(status=404,body='Not found')
  page.route('**/*',handle)
  page.goto(args.base_url or 'https://stories.test/')
  page.locator('#candidateGrid .candidate-card').nth(2).wait_for()
  assert page.locator('#candidateGrid .candidate-card').count()==3
  page.locator('[data-global-filter="source"]').select_option('Agency')
  assert page.locator('#candidateGrid .candidate-card').count()==1
  page.locator('#searchInput').fill('Agency')
  page.locator('[data-view="phase2"]').click()
  assert page.locator('#searchInput').input_value()==''
  assert page.locator('#storyGrid .story-card').count()==3
  assert page.locator('[data-global-filter="source"]').count()==0
  page.locator('[data-global-filter="setup"]').select_option('failed')
  assert page.locator('#storyGrid .story-card').count()==1
  assert page.locator('[data-phase2-retry="3"]').is_visible()
  page.locator('#searchInput').fill('no such story')
  assert page.locator('#emptyState').is_visible()
  page.locator('#emptyState [data-clear-filters]').click()
  assert page.locator('#viewHeading').inner_text()=='Phase 2'
  assert page.locator('#storyGrid .story-card').count()==3
  page.locator('[data-view="problems"]').click()
  assert page.locator('#candidateGrid .candidate-card').count()==2
  assert page.locator('#storyGrid .story-card').count()==1
  assert page.locator('#sourceManagementGrid .source-management-card').count()==1
  page.locator('[data-global-filter="problem"]').select_option('source')
  assert not page.locator('#candidateSection').is_visible()
  assert page.locator('#sourceManagementSection').is_visible()
  page.locator('[data-view="sources"]').click()
  page.locator('[data-global-filter="sourceStatus"]').select_option('disabled')
  assert page.locator('#sourceManagementGrid .source-management-card').count()==1
  page.locator('[data-view="candidates"]').click()
  assert page.locator('#candidateGrid .candidate-card').count()==3
  page.locator('.advanced-filters summary').click()
  page.locator('[data-global-filter="screening"]').select_option('EXCLUDED')
  assert page.locator('#candidateGrid .candidate-card').count()==1
  page.locator('[data-view="phase3"]').click()
  assert page.locator('#storyGrid .story-card').count()==1
  page.locator('[data-view="candidates"]').click()
  assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'horizontal overflow'
  page.screenshot(path=f'/tmp/categories-{width}.png',full_page=True)
  assert not errors,errors
  print(f'Browser checks passed at {width}px; no JavaScript errors or horizontal overflow')
  page.close()
 browser.close()
