"""Versioned official-source definitions for the second appellate batch."""
from urllib.parse import urlencode

ORDINALS=['First','Second','Third','Fourth','Fifth','Sixth','Seventh','Eighth','Ninth','Tenth','Eleventh','Twelfth','Thirteenth','Fourteenth','Fifteenth']


def sources():
    specs=[]
    for number,label in enumerate(['Supreme']+ORDINALS[:6]):
        court_id='fla' if number==0 else f'fladistctapp{number}'
        scope='supreme_court' if number==0 else label.lower()+'_district_court_of_appeal'
        specs.append(dict(name='Florida Supreme Court opinions' if number==0 else f'Florida {label} District Court of Appeal opinions',
            court_id=court_id,jurisdiction='US-FL',url='https://flcourts-media.flcourts.gov/_search/opinions/',
            collector_type='json',hosts=['flcourts-media.flcourts.gov'],pattern=r'/content/download/\d+/opinion/[^?#]+\.pdf$',
            scope='Official opinion-document API; rolling 30-day discovery window, all result pages and publication types.',
            config=dict(listing_adapter='florida',scope=scope,siteaccess='supreme2' if number==0 else f'{number}dca')))
    ca=[('cal','103','Supreme Court'),('calctapp1d','100','First District Court of Appeal'),('calctapp2d','102','Second District Court of Appeal'),('calctapp3d','107','Third District Court of Appeal'),('calctapp4d','106','Fourth District Court of Appeal Division One'),('calctapp4d','98','Fourth District Court of Appeal Division Two'),('calctapp4d','101','Fourth District Court of Appeal Division Three'),('calctapp5d','99','Fifth District Court of Appeal'),('calctapp6d','105','Sixth District Court of Appeal')]
    for court_id,taxonomy,label in ca:
        specs.append(dict(name=f'California {label} published opinions',court_id=court_id,jurisdiction='US-CA',
            url='https://courts.ca.gov/opinions/publishedcitable-opinions?'+urlencode(dict(field_opinion_source_target_id=taxonomy,items_per_page=200)),
            collector_type='json',hosts=['courts.ca.gov','www.courts.ca.gov','www4.courts.ca.gov'],
            pattern=r'/opinions/documents/[^/?]+\.pdf$',scope='Official published/citable opinions listing, all result pages; superior-court appellate divisions and unpublished opinions outside this source.',
            config=dict(listing_adapter='california',california_court=taxonomy)))
    tx=[('tex','cossup','Supreme Court'),('texcrimapp','coscca','Court of Criminal Appeals')]+[(f'texapp{n:02}',f'coa{n:02}',f'{label} Court of Appeals') for n,label in enumerate(ORDINALS,1)]
    for court_id,code,label in tx:
        specs.append(dict(name=f'Texas {label} opinions',court_id=court_id,jurisdiction='US-TX',
            url='https://search.txcourts.gov/docketsrch.aspx?coa='+code,collector_type='json',hosts=['search.txcourts.gov'],
            pattern=r'/SearchMedia\.aspx\?.*\bDT=Opinion(?:&|$)',scope='Official release calendar and dated releases; opinion documents only, rolling 30-day discovery window within currently exposed quarter. Previous-quarter outage recovery remains outstanding.',
            config=dict(listing_adapter='texas',texas_court=code)))
    return specs


def pending_new_york():
    return [dict(court_id=id,url='https://www.nycourts.gov/reporter/RSS/'+feed+'.xml') for id,feed in
            [('ny','COA'),('nyappdiv1','AD1st'),('nyappdiv2','AD2d'),('nyappdiv3','AD3d'),('nyappdiv4','AD4th'),('nyappterm1','AT1'),('nyappterm2','AT2')]]
