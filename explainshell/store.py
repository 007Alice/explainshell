'''data objects to save processed man pages to mongodb'''
import pymongo, collections, re, logging

from explainshell import errors, util

logger = logging.getLogger(__name__)

class classifiermanpage(collections.namedtuple('classifiermanpage', 'name paragraphs')):
    '''a man page that had its paragraphs manually tagged as containing options
    or not'''
    @staticmethod
    def from_store(d):
        m = classifiermanpage(d['name'], [paragraph.from_store(p) for p in d['paragraphs']])
        return m

    def to_store(self):
        return {'name' : self.name,
                'paragraphs' : [p.to_store() for p in self.paragraphs]}

class paragraph(object):
    '''a paragraph inside a man page is text that ends with two new lines'''
    def __init__(self, idx, text, section, is_option):
        self.idx = idx
        self.text = text
        self.section = section
        self.is_option = is_option

    def cleantext(self):
        t = re.sub(r'<[^>]+>', '', self.text)
        t = re.sub('&lt;', '<', t)
        t = re.sub('&gt;', '>', t)
        return t

    @staticmethod
    def from_store(d):
        p = paragraph(d.get('idx', 0), d['text'].encode('utf8'), d['section'], d['is_option'])
        return p

    def to_store(self):
        return {'idx' : self.idx, 'text' : self.text, 'section' : self.section,
                'is_option' : self.is_option}

    def __repr__(self):
        t = self.cleantext()
        t = t[:min(20, t.find('\n'))].lstrip()
        return '<paragraph %d, %s: %r>' % (self.idx, self.section, t)

    def __eq__(self, other):
        if not other:
            return False
        return self.__dict__ == other.__dict__

class option(paragraph):
    '''a paragraph that contains extracted options

    short is a list of short options (-a, -b, ..)
    long is a list of long options (--a, --b)
    expectsarg specifies if one of the short/long options expects an additional argument
    argument specifies if to consider this as positional arguments
    '''
    def __init__(self, p, short, long, expectsarg, argument=None):
        paragraph.__init__(self, p.idx, p.text, p.section, p.is_option)
        self.short = short
        self.long = long
        self._opts = self.short + self.long
        self.argument = argument
        self.expectsarg = expectsarg

    @property
    def opts(self):
        return self._opts

    @classmethod
    def from_store(cls, d):
        p = paragraph.from_store(d)

        return cls(p, d['short'], d['long'], d['expectsarg'], d['argument'])

    def to_store(self):
        d = paragraph.to_store(self)
        assert d['is_option']
        d['short'] = self.short
        d['long'] = self.long
        d['expectsarg'] = self.expectsarg
        d['argument'] = self.argument
        return d

    def __str__(self):
        return '(%s)' % ', '.join([str(x) for x in self.opts])

    def __repr__(self):
        return '<options for paragraph %d: %s>' % (self.idx, str(self))

class manpage(object):
    '''processed man page

    source - the path to the original source man page
    name - the name of this man page as extracted by manpage.manpage
    synopsis - the synopsis of this man page as extracted by manpage.manpage
    paragraphs - a list of paragraphs (and options) that contain all of the text and options
        extracted from this man page
    aliases - a list of aliases found for this man page
    partialmatch - allow interperting options without a leading '-'
    multicommand - consider sub commands when explaining a command with this man page,
        e.g. git -> git commit
    updated - whether this man page was manually updated
    '''
    def __init__(self, source, name, synopsis, paragraphs, aliases,
                 partialmatch=False, multicommand=False, updated=False):
        self.source = source
        self.name = name
        self.synopsis = synopsis
        self.paragraphs = paragraphs
        self.aliases = aliases
        self.partialmatch = partialmatch
        self.multicommand = multicommand
        self.updated = updated

    def removeoption(self, idx):
        for i, p in self.paragraphs:
            if p.idx == idx:
                if not isinstance(p, option):
                    raise ValueError("paragraph %d isn't an option" % idx)
                self.paragraphs[i] = paragraph(p.idx, p.text, p.section, False)
                return
        raise ValueError('idx %d not found' % idx)

    @property
    def namesection(self):
        name, section = util.namesection(self.source[:-3])
        return '%s(%s)' % (name, section)

    @property
    def section(self):
        name, section = util.namesection(self.source[:-3])
        return section

    @property
    def options(self):
        return [p for p in self.paragraphs if isinstance(p, option)]

    @property
    def arguments(self):
        # go over all paragraphs and look for those with the same 'argument'
        # field
        groups = collections.OrderedDict()
        for opt in self.options:
            if opt.argument:
                groups.setdefault(opt.argument, []).append(opt)

        # merge all the paragraphs under the same argument to a single string
        for k, l in groups.iteritems():
            groups[k] = '\n\n'.join([p.text for p in l])

        return groups

    @property
    def synopsisnoname(self):
        return re.match(r'[\w|-]+ - (.*)$', self.synopsis).group(1)

    def find_option(self, flag):
        for option in self.options:
            for o in option.opts:
                if o == flag:
                    return option

    def to_store(self):
        return {'source' : self.source, 'name' : self.name, 'synopsis' : self.synopsis,
                'paragraphs' : [p.to_store() for p in self.paragraphs],
                'aliases' : self.aliases, 'partialmatch' : self.partialmatch,
                'multicommand' : self.multicommand, 'updated' : self.updated}

    @staticmethod
    def from_store(d):
        paragraphs = []
        for pd in d['paragraphs']:
            pp = paragraph.from_store(pd)
            if pp.is_option == True and 'short' in pd:
                pp = option.from_store(pd)
            paragraphs.append(pp)

        return manpage(d['source'], d['name'], d['synopsis'], paragraphs,
                       [tuple(x) for x in d['aliases']], d['partialmatch'],
                       d['multicommand'], d['updated'])

    def __repr__(self):
        return '<manpage %r(%s), %d options>' % (self.name, self.section, len(self.options))

class store(object):
    '''read/write processed man pages from mongodb

    we use three collections:
    1) classifier - contains manually tagged paragraphs from man pages
    2) manpage - contains a processed man page
    3) mapping - contains (name, manpageid, score) tuples
    '''
    def __init__(self, db='explainshell', host='localhost'):
        logger.info('creating store, db = %r, host = %r', db, host)
        self.connection = pymongo.MongoClient(host)
        self.db = self.connection[db]
        self.classifier = self.db['classifier']
        self.manpage = self.db['manpage']
        self.mapping = self.db['mapping']

    def close(self):
        self.connection.disconnect()
        self.classifier = self.manpage = self.mapping = self.db = None

    def drop(self, confirm=False):
        if not confirm:
            return

        logger.info('dropping mapping, manpage, collections')
        self.mapping.drop()
        self.manpage.drop()

    def trainingset(self):
        for d in self.classifier.find():
            yield classifiermanpage.from_store(d)

    def __contains__(self, name):
        c = self.mapping.find({'src' : name}).count()
        return c > 0

    def __iter__(self):
        for d in self.manpage.find():
            yield manpage.from_store(d)

    def findmanpage(self, name, section=None):
        '''find a man page by its name, optionally in the specified section

        we return the man page found with the highest score'''
        if name.endswith('.gz'):
            logger.info('name ends with .gz, looking up an exact match by source')
            d = self.manpage.find_one({'source':name})
            if not d:
                raise errors.ProgramDoesNotExist(name)
            m = manpage.from_store(d)
            logger.info('returning %s', m)
            return [m]

        logger.info('looking up manpage in mapping with src %r', name)
        cursor = self.mapping.find({'src' : name})
        count = cursor.count()
        if not count:
            raise errors.ProgramDoesNotExist(name)

        dsts = dict(((d['dst'], d['score']) for d in cursor))
        cursor = self.manpage.find({'_id' : {'$in' : list(dsts.keys())}})
        if cursor.count() != len(dsts):
            logger.error('one of %r mappings is missing in manpage collection '
                         '(%d mappings, %d found)', dsts, len(dsts), cursor.count())
        results = [(d['_id'], manpage.from_store(d)) for d in cursor]
        results.sort(key=lambda x: dsts.get(x[0], 0), reverse=True)
        results = [x[1] for x in results]
        logger.info('got %s', results)
        if section is not None:
            if len(results) > 1:
                results.sort(key=lambda m: m.section == section, reverse=True)
                logger.info(r'sorting %r so %s is first', results, section)
            if not results[0].section == section:
                raise errors.ProgramDoesNotExist('%s.%s' % (name, section))
        return results

    def addmapping(self, src, dst, score):
        self.mapping.insert({'src' : src, 'dst' : dst, 'score' : score})

    def addmanpage(self, m):
        '''add m into the store, if it exists first remove it and its mappings

        each man page may have aliases besides the name determined by its
        basename'''
        d = self.manpage.find_one({'source' : m.source})
        if d:
            logger.info('removing old manpage %s (%s)', m.source, d['_id'])
            self.manpage.remove(d['_id'])

            # remove old mappings if there are any
            c = self.mapping.count()
            self.mapping.remove({'dst' : d['_id']})
            c -= self.mapping.count()
            logger.info('removed %d mappings for manpage %s', c, m.source)

        o = self.manpage.insert(m.to_store())

        for alias, score in m.aliases:
            self.addmapping(alias, o, score)
            logger.info('inserting mapping (alias) %s -> %s (%s) with score %d', alias, m.name, o, score)
        return m

    def updatemanpage(self, m):
        '''update m and add new aliases if necessary

        change updated attribute so we don't overwrite this in the future'''
        logger.info('updating manpage %s', m.source)
        m.updated = True
        self.manpage.update({'source' : m.source}, m.to_store())
        _id = self.manpage.find_one({'source' : m.source}, fields={'_id':1})['_id']
        for alias, score in m.aliases:
            if alias not in self:
                self.addmapping(alias, _id, score)
                logger.info('inserting mapping (alias) %s -> %s (%s) with score %d', alias, m.name, _id, score)
            else:
                logger.debug('mapping (alias) %s -> %s (%s) already exists', alias, m.name, _id)
        return m

    def verify(self):
        # check that everything in manpage is reachable
        mappings = list(self.mapping.find())
        reachable = set([m['dst'] for m in mappings])
        manpages = set([m['_id'] for m in self.manpage.find(fields={'_id':1})])

        ok = True
        unreachable = manpages - reachable
        if unreachable:
            logger.error('manpages %r are unreachable (nothing maps to them)', unreachable)
            unreachable = [self.manpage.find_one({'_id' : u})['name'] for u in unreachable]
            ok = False

        notfound = reachable - manpages
        if notfound:
            logger.error('mappings to inexisting manpages: %r', notfound)
            ok = False

        return ok, unreachable, notfound

    def names(self):
        cursor = self.manpage.find(fields={'name':1})
        for d in cursor:
            yield d['_id'], d['name']

    def mappings(self):
        cursor = self.mapping.find(fields={'src':1})
        for d in cursor:
            yield d['src'], d['_id']

    def setmulticommand(self, manpageid):
        self.manpage.update({'_id' : manpageid}, {'$set' : {'multicommand' : True}})
